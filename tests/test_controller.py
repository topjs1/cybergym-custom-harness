from __future__ import annotations

import base64
import csv

import pytest
from a2a.types import DataPart, FilePart

from cybergym_harness.controller import HarnessSession
from cybergym_harness.metrics import MetricsWriter
from tests.helpers import challenge_message


class FakeUpdater:
    def __init__(self, session: HarnessSession):
        self.session = session
        self.requests = 0
        self.artifacts = []
        self.statuses = []

    async def update_status(self, state, message):
        self.statuses.append((state, message))

    def new_agent_message(self, parts):
        class Wrapper:
            def __init__(self, parts):
                self.parts = parts
        return Wrapper(parts)

    async def requires_input(self, message):
        self.requests += 1
        file_part = next(p.root for p in message.parts if isinstance(p.root, FilePart))
        poc = base64.b64decode(file_part.file.bytes)
        exit_code = 1 if poc == b"mock-crash-poc" else 0
        await self.session.feedback_queue.put({"exit_code": exit_code, "output": "mock verifier"})

    async def add_artifact(self, parts, name):
        self.artifacts.append((parts, name))


@pytest.mark.asyncio
async def test_mock_llm_feedback_loop_and_metrics(settings):
    writer = MetricsWriter(settings.results_csv, settings.events_jsonl)
    session = HarnessSession("ctx-feedback", settings, writer)
    updater = FakeUpdater(session)
    await session.run(challenge_message(), updater)
    assert session.success
    assert updater.requests == 2
    assert len(updater.artifacts) == 1
    assert session.metrics.attempts == 2
    assert session.metrics.input_tokens == 180
    assert session.metrics.output_tokens == 35
    with settings.results_csv.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["status"] == "PASS"
    assert rows[0]["task_id"] == "mock:0001"
    assert rows[0]["usage_source"] == "mock"
    assert rows[0]["api_cost_source"] == "unavailable"

