from __future__ import annotations

import base64
import csv
import json
from dataclasses import replace

import pytest
from a2a.types import DataPart, FilePart

from cybergym_harness import controller as controller_module
from cybergym_harness.controller import HarnessSession
from cybergym_harness.llm import ChatTurn
from cybergym_harness.metrics import MetricsWriter, TaskMetrics, UsageRecord
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
    assert "solver" in rows[0]["role_tokens_json"]
    assert "solve" in rows[0]["phase_seconds_json"]


@pytest.mark.asyncio
async def test_mutation_auto_exploration_runs_without_llm(settings):
    configured = replace(settings, mutation_attempts=2)
    writer = MetricsWriter(configured.results_csv, configured.events_jsonl)
    session = HarnessSession("ctx-mutation", configured, writer)
    updater = FakeUpdater(session)
    await session.run(challenge_message(), updater)
    assert session.success
    assert session.metrics.attempts >= 2
    assert any(event["event"] == "poc_mutation" for event in session.metrics.events)


class ProseThenSubmitLLM:
    """Emits prose-only turns first, then submits a crashing candidate."""

    def __init__(self, prose_turns: int = 2):
        self.prose_turns = prose_turns
        self.calls = 0

    async def complete(self, messages, tools, max_output):
        self.calls += 1
        usage = UsageRecord(5, 2, 0, 5, 7, "mock", 0.0)
        if self.calls <= self.prose_turns:
            return ChatTurn({"role": "assistant", "content": "Looks fine, no further action."}, usage, "mock")
        return ChatTurn({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call-{self.calls}",
                "function": {
                    "name": "submit_poc",
                    "arguments": json.dumps({"poc_base64": base64.b64encode(b"mock-crash-poc").decode("ascii")}),
                },
            }],
        }, usage, "mock")

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_prose_only_turns_are_nudged_until_submission(monkeypatch, settings):
    scripted = ProseThenSubmitLLM(prose_turns=2)
    monkeypatch.setattr(controller_module, "make_llm", lambda _settings: scripted)
    writer = MetricsWriter(settings.results_csv, settings.events_jsonl)
    session = HarnessSession("ctx-nudge", settings, writer)
    updater = FakeUpdater(session)
    await session.run(challenge_message(), updater)
    assert session.success
    assert session.metrics.attempts == 1
    assert any(event["event"] == "prose_only_turn" for event in session.metrics.events)


@pytest.mark.asyncio
async def test_gives_up_after_max_resume_nudges(monkeypatch, settings):
    class AlwaysProse:
        async def complete(self, messages, tools, max_output):
            return ChatTurn({"role": "assistant", "content": "done"}, UsageRecord(1, 1, 0, 1, 2, "mock", 0.0), "mock")

        async def close(self):
            return None

    configured = replace(settings, max_resume_nudges=2)
    monkeypatch.setattr(controller_module, "make_llm", lambda _settings: AlwaysProse())
    writer = MetricsWriter(configured.results_csv, configured.events_jsonl)
    session = HarnessSession("ctx-giveup", configured, writer)
    updater = FakeUpdater(session)
    await session.run(challenge_message(), updater)
    assert not session.success
    nudges = [event for event in session.metrics.events if event["event"] == "prose_only_turn"]
    assert len(nudges) == 3  # max_resume_nudges + the turn that trips the limit


class AlwaysSubmitLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools, max_output):
        self.calls += 1
        return ChatTurn({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"c{self.calls}",
                "function": {
                    "name": "submit_poc",
                    "arguments": json.dumps({"poc_base64": base64.b64encode(b"mock-crash-poc").decode("ascii")}),
                },
            }],
        }, UsageRecord(3, 2, 0, 3, 5, "mock", 0.0), "mock")

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_ensemble_tracks_share_single_submission(monkeypatch, settings):
    configured = replace(settings, tracks=2)
    monkeypatch.setattr(controller_module, "make_llm", lambda _settings: AlwaysSubmitLLM())
    writer = MetricsWriter(configured.results_csv, configured.events_jsonl)
    session = HarnessSession("ctx-ensemble", configured, writer)
    updater = FakeUpdater(session)
    await session.run(challenge_message(), updater)
    assert session.success
    assert session.metrics.attempts == 1
    assert len(updater.artifacts) == 1
    roles = {event.get("role") for event in session.metrics.events if event["event"] == "poc_attempt"}
    assert roles


class BuildThenSubmitWithGeneratedPrefixLLM:
    """Submits with a 'generated/'-prefixed path, as real models sometimes do."""

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools, max_output):
        self.calls += 1
        if self.calls == 1:
            name, args = "build_poc", {"operations": [{"type": "hex", "value": "41424344"}]}
        else:
            name, args = "submit_poc", {"path": "generated/poc", "explanation": "prefix path"}
        return ChatTurn({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": f"c{self.calls}", "function": {"name": name, "arguments": json.dumps(args)}}],
        }, UsageRecord(5, 2, 0, 5, 7, "mock", 0.0), "mock")

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_submit_path_accepts_generated_prefix(monkeypatch, settings):
    monkeypatch.setattr(controller_module, "make_llm", lambda _settings: BuildThenSubmitWithGeneratedPrefixLLM())
    writer = MetricsWriter(settings.results_csv, settings.events_jsonl)
    session = HarnessSession("ctx-prefix", settings, writer)
    updater = FakeUpdater(session)
    await session.run(challenge_message(), updater)
    assert updater.requests >= 1
    assert any(event["event"] == "poc_attempt" for event in session.metrics.events)


@pytest.mark.asyncio
async def test_cost_cap_stops_run(monkeypatch, settings):
    class HeavyUsageLLM:
        async def complete(self, messages, tools, max_output):
            usage = UsageRecord(1_000_000, 100_000, 0, 1_000_000, 1_100_000, "api_actual", 0.0)
            return ChatTurn({"role": "assistant", "content": "thinking..."}, usage, "deepseek-flash")

        async def close(self):
            return None

    configured = replace(
        settings,
        llm_mode="deepseek",
        model="deepseek-flash",
        pricing_tier="off_peak",
        max_cost_usd=0.1,
    )
    monkeypatch.setattr(controller_module, "make_llm", lambda _settings: HeavyUsageLLM())
    writer = MetricsWriter(configured.results_csv, configured.events_jsonl)
    session = HarnessSession("ctx-cost", configured, writer)
    updater = FakeUpdater(session)
    await session.run(challenge_message(), updater)
    assert "cost cap reached" in session.metrics.error


class ExploreThenSubmitLLM:
    def __init__(self, explore_calls: int):
        self.explore_calls = explore_calls
        self.calls = 0

    async def complete(self, messages, tools, max_output):
        self.calls += 1
        usage = UsageRecord(5, 2, 0, 5, 7, "mock", 0.0)
        if self.calls <= self.explore_calls:
            return ChatTurn({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{self.calls}", "function": {"name": "list_files", "arguments": "{}"}}],
            }, usage, "mock")
        return ChatTurn({
            "role": "assistant", "content": None,
            "tool_calls": [{"id": f"c{self.calls}", "function": {
                "name": "submit_poc",
                "arguments": json.dumps({"poc_base64": base64.b64encode(b"mock-crash-poc").decode("ascii")}),
            }}],
        }, usage, "mock")

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_force_submit_directive_then_success(monkeypatch, settings):
    configured = replace(settings, force_submit_after=2)
    monkeypatch.setattr(controller_module, "make_llm", lambda _settings: ExploreThenSubmitLLM(explore_calls=4))
    writer = MetricsWriter(configured.results_csv, configured.events_jsonl)
    session = HarnessSession("ctx-force", configured, writer)
    updater = FakeUpdater(session)
    await session.run(challenge_message(), updater)
    assert session.success
    assert any(event["event"] == "force_submit" for event in session.metrics.events)


class BuildThenGiveUpLLM:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools, max_output):
        self.calls += 1
        usage = UsageRecord(5, 2, 0, 5, 7, "mock", 0.0)
        if self.calls == 1:
            return ChatTurn({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "b1", "function": {
                    "name": "build_poc",
                    "arguments": json.dumps({"operations": [{"type": "hex", "value": "41424344"}]}),
                }}],
            }, usage, "mock")
        return ChatTurn({"role": "assistant", "content": "analysis complete, nothing more to add"}, usage, "mock")

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_fallback_artifact_when_model_never_submits(monkeypatch, settings):
    configured = replace(settings, force_submit_after=99, max_resume_nudges=1)
    monkeypatch.setattr(controller_module, "make_llm", lambda _settings: BuildThenGiveUpLLM())
    writer = MetricsWriter(configured.results_csv, configured.events_jsonl)
    session = HarnessSession("ctx-fallback", configured, writer)
    updater = FakeUpdater(session)
    await session.run(challenge_message(), updater)
    assert not session.success
    assert len(updater.artifacts) == 1
    assert session.last_poc == b"ABCD"
    assert any(event["event"] == "fallback_artifact" for event in session.metrics.events)


def test_phase_and_role_tagging():
    metrics = TaskMetrics(task_id="t", task_id_source="x", model="mock", pricing_tier="unknown")
    metrics.phase_begin("solve")
    metrics.add_usage(UsageRecord(10, 5, 0, 10, 15, "mock", 0.0, role="recon"))
    metrics.phase_end("solve")
    row = metrics.csv_row()
    assert "recon" in row["role_tokens_json"]
    assert "solve" in row["phase_seconds_json"]

