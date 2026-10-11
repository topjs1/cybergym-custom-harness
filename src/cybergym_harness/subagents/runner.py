from __future__ import annotations

import json
from typing import Any

from ..code_tools import CodeTools
from ..llm import LLMAdapter
from ..metrics import TaskMetrics
from ..prompts import READONLY_TOOLS


async def run_readonly_subagent(
    *,
    llm: LLMAdapter,
    code_tools: CodeTools,
    role: str,
    system_prompt: str,
    user_prompt: str,
    max_turns: int,
    max_output_tokens: int,
    metrics: TaskMetrics,
    tool_output_limit: int = 6000,
) -> str:
    phase = f"subagent_{role}"
    metrics.phase_begin(phase)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_content = ""
    try:
        for _ in range(max(1, max_turns)):
            turn = await llm.complete(messages, READONLY_TOOLS, max_output_tokens)
            metrics.add_usage(turn.usage, role=role, phase=phase)
            message = turn.message
            messages.append(message)
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                last_content = content.strip()
            calls = message.get("tool_calls") or []
            if not calls:
                return last_content
            for call in calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                try:
                    args = json.loads(function.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                    output = await code_tools.execute_readonly_tool(name, args)
                except Exception as exc:  # noqa: BLE001 - returned to the subagent
                    output = f"tool error ({type(exc).__name__}): {exc}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or name),
                    "content": output[:tool_output_limit],
                })
        return last_content
    finally:
        metrics.phase_end(phase)
