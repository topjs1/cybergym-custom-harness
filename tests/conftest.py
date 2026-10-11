from __future__ import annotations

from pathlib import Path

import pytest

from cybergym_harness.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="",
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        thinking="disabled",
        temperature=0.0,
        max_output_tokens=4096,
        task_timeout_seconds=30,
        token_budget=100_000,
        max_attempts=4,
        llm_timeout_seconds=5,
        feedback_timeout_seconds=5,
        work_root=tmp_path / "tasks",
        results_csv=tmp_path / "results.csv",
        events_jsonl=tmp_path / "events.jsonl",
        python_backend="disabled",
        sandbox_image="python:3.13.9-slim-bookworm",
        sandbox_timeout_seconds=5,
        pricing_tier="off_peak",
        max_attachment_bytes=4 * 1024 * 1024,
        max_extracted_bytes=16 * 1024 * 1024,
        max_poc_bytes=1024 * 1024,
        max_tool_output_chars=12_000,
        llm_mode="mock",
    )

