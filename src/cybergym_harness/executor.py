from __future__ import annotations

import sys
import traceback

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import InvalidRequestError, TaskState, UnsupportedOperationError
from a2a.utils import new_agent_text_message, new_task
from a2a.utils.errors import ServerError

from .controller import TaskManager


TERMINAL_STATES = {
    TaskState.completed,
    TaskState.canceled,
    TaskState.failed,
    TaskState.rejected,
}


class HarnessExecutor(AgentExecutor):
    def __init__(self, manager: TaskManager):
        self.manager = manager

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        if message is None:
            raise ServerError(error=InvalidRequestError(message="missing A2A message"))
        task = context.current_task

        # Feedback is allowed to re-enter while the initial request is blocked on
        # the per-context queue. Route it before the terminal-state guard because
        # requires_input/continuation transitions are represented on the same task.
        feedback_context_id = task.context_id if task else message.context_id
        if feedback_context_id and self.manager.looks_like_feedback(message):
            session = self.manager.active_session(feedback_context_id)
            if session is not None:
                if task is None:
                    task = new_task(message)
                    await event_queue.enqueue_event(task)
                updater = TaskUpdater(event_queue, task.id, task.context_id)
                await updater.start_work()
                await session.deliver_feedback(message)
                await updater.complete()
                return

        if task and task.status.state in TERMINAL_STATES:
            raise ServerError(
                error=InvalidRequestError(
                    message=f"task {task.id} already processed ({task.status.state})"
                )
            )
        if task is None:
            task = new_task(message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work()
        try:
            await self.manager.start(task.context_id, message, updater)
            if not updater._terminal_state_reached:
                await updater.complete()
        except ValueError as exc:
            await updater.reject(
                new_agent_text_message(
                    f"invalid CyberGym challenge: {exc}",
                    context_id=task.context_id,
                    task_id=task.id,
                )
            )
        except Exception as exc:
            print("Harness task failed:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            await updater.failed(
                new_agent_text_message(
                    f"harness error: {type(exc).__name__}: {exc}",
                    context_id=task.context_id,
                    task_id=task.id,
                )
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())

