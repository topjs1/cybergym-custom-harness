from __future__ import annotations

import argparse

import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Task

from .config import Settings
from .controller import TaskManager
from .executor import HarnessExecutor


class NoAttachmentHistoryTaskStore(InMemoryTaskStore):
    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        task.history = None
        await super().save(task, context)


def build_application(settings: Settings, card_url: str) -> object:
    skill = AgentSkill(
        id="cybergym_poc",
        name="CyberGym PoC generation",
        description="Analyzes CyberGym source attachments and submits a raw crash PoC.",
        tags=["cybersecurity", "vulnerability-reproduction", "poc"],
        examples=[],
    )
    card = AgentCard(
        name="CyberGym Custom Harness",
        description="Independent, budgeted DeepSeek Purple Agent for CyberGym.",
        url=card_url,
        version="0.1.0",
        default_input_modes=["text", "application/octet-stream"],
        default_output_modes=["text", "application/octet-stream"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill],
    )
    handler = DefaultRequestHandler(
        agent_executor=HarnessExecutor(TaskManager(settings)),
        task_store=NoAttachmentHistoryTaskStore(),
    )
    return A2AStarletteApplication(
        agent_card=card,
        http_handler=handler,
        max_content_length=None,
    ).build()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CyberGym custom Purple Agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9009)
    parser.add_argument("--card-url")
    parser.add_argument("--uds", help="Bind a Unix domain socket (local testing)")
    args = parser.parse_args()
    settings = Settings.from_env()
    card_url = args.card_url or f"http://{args.host}:{args.port}/"
    application = build_application(settings, card_url)
    if args.uds:
        uvicorn.run(application, uds=args.uds)
    else:
        uvicorn.run(application, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

