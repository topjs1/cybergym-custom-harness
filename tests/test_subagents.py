from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cybergym_harness.code_tools import CodeTools
from cybergym_harness.llm import ChatTurn
from cybergym_harness.metrics import TaskMetrics, UsageRecord
from cybergym_harness.subagents import run_recon, run_reviewer, run_readonly_subagent


class ScriptedLLM:
    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = 0

    async def complete(self, messages, tools, max_tokens):
        turn = self._turns[self.calls] if self.calls < len(self._turns) else self._turns[-1]
        self.calls += 1
        return turn

    async def close(self):
        return None


def _turn(message, role="recon"):
    usage = UsageRecord(10, 5, 0, 10, 15, "mock", 0.0, role=role, phase="")
    return ChatTurn(message, usage, "mock")


def _metrics():
    return TaskMetrics(task_id="t", task_id_source="x", model="mock", pricing_tier="unknown")


def _tools(tmp_path: Path) -> CodeTools:
    repo = tmp_path / "repo-vul"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "a.c").write_text("int parse(const char *p){ return p[0]; }\n")
    return CodeTools(repo)


@pytest.mark.asyncio
async def test_readonly_subagent_runs_tools_then_returns(settings, tmp_path):
    tools = _tools(tmp_path)
    turns = [
        _turn({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "c1",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.c"})},
            }],
        }),
        _turn({"role": "assistant", "content": "final hypothesis"}),
    ]
    metrics = _metrics()
    output = await run_readonly_subagent(
        llm=ScriptedLLM(turns),
        code_tools=tools,
        role="recon",
        system_prompt="s",
        user_prompt="u",
        max_turns=4,
        max_output_tokens=128,
        metrics=metrics,
    )
    assert output == "final hypothesis"
    assert metrics.role_usage["recon"]["api_calls"] == 2
    assert "subagent_recon" in metrics.phase_seconds


@pytest.mark.asyncio
async def test_run_recon_and_reviewer_return_text(settings, tmp_path):
    tools = _tools(tmp_path)
    configured = replace(settings, recon_subagent=True, reviewer_subagent=True, recon_max_turns=2, subagent_output_tokens=128)
    recon_llm = ScriptedLLM([_turn({"role": "assistant", "content": '{"vuln_class": "heap-buffer-overflow"}'})])
    recon = await run_recon(
        llm=recon_llm,
        settings=configured,
        code_tools=tools,
        triage_summary="triage",
        green_instruction="go",
        metrics=_metrics(),
    )
    assert "heap-buffer-overflow" in recon

    reviewer_llm = ScriptedLLM([_turn({"role": "assistant", "content": '[{"vuln_class": "integer-overflow"}]'}, role="reviewer")])
    review = await run_reviewer(
        llm=reviewer_llm,
        settings=configured,
        code_tools=tools,
        triage_summary="triage",
        attempts_summary="attempt 1: no_crash",
        metrics=_metrics(),
    )
    assert "integer-overflow" in review
