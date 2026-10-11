from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    thinking: str
    temperature: float
    max_output_tokens: int
    task_timeout_seconds: int
    token_budget: int
    max_attempts: int
    llm_timeout_seconds: int
    feedback_timeout_seconds: int
    work_root: Path
    results_csv: Path
    events_jsonl: Path
    python_backend: str
    sandbox_image: str
    sandbox_timeout_seconds: int
    pricing_tier: str
    max_attachment_bytes: int
    max_extracted_bytes: int
    max_poc_bytes: int
    max_tool_output_chars: int
    llm_mode: str

    @classmethod
    def from_env(cls) -> "Settings":
        thinking = os.getenv("DEEPSEEK_THINKING", "enabled").strip().lower()
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("DEEPSEEK_THINKING must be enabled or disabled")
        backend = os.getenv("HARNESS_PYTHON_BACKEND", "disabled").strip().lower()
        if backend not in {"disabled", "docker", "mock"}:
            raise ValueError("HARNESS_PYTHON_BACKEND must be disabled, docker, or mock")
        tier = os.getenv("DEEPSEEK_PRICING_TIER", "off_peak").strip().lower()
        if tier not in {"off_peak", "peak", "unknown"}:
            raise ValueError("DEEPSEEK_PRICING_TIER must be off_peak, peak, or unknown")
        llm_mode = os.getenv("HARNESS_LLM_MODE", "deepseek").strip().lower()
        if llm_mode not in {"deepseek", "mock"}:
            raise ValueError("HARNESS_LLM_MODE must be deepseek or mock")
        return cls(
            api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip(),
            thinking=thinking,
            temperature=_env_float("DEEPSEEK_TEMPERATURE", 0.0),
            max_output_tokens=_env_int("DEEPSEEK_MAX_OUTPUT_TOKENS", 4096, 1),
            task_timeout_seconds=_env_int("HARNESS_TASK_TIMEOUT_SECONDS", 1800, 1),
            token_budget=_env_int("HARNESS_TOKEN_BUDGET", 100_000, 1),
            max_attempts=_env_int("HARNESS_MAX_ATTEMPTS", 30, 1),
            llm_timeout_seconds=_env_int("HARNESS_LLM_TIMEOUT_SECONDS", 180, 1),
            feedback_timeout_seconds=_env_int("HARNESS_FEEDBACK_TIMEOUT_SECONDS", 60, 1),
            work_root=Path(os.getenv("HARNESS_WORK_ROOT", "/tmp/cybergym-custom-harness/tasks")),
            results_csv=Path(os.getenv("HARNESS_RESULTS_CSV", "/tmp/cybergym-custom-harness/results.csv")),
            events_jsonl=Path(os.getenv("HARNESS_EVENTS_JSONL", "/tmp/cybergym-custom-harness/events.jsonl")),
            python_backend=backend,
            sandbox_image=os.getenv("HARNESS_SANDBOX_IMAGE", "python:3.13.9-slim-bookworm"),
            sandbox_timeout_seconds=_env_int("HARNESS_SANDBOX_TIMEOUT_SECONDS", 20, 1),
            pricing_tier=tier,
            max_attachment_bytes=_env_int("HARNESS_MAX_ATTACHMENT_BYTES", 268_435_456, 1),
            max_extracted_bytes=_env_int("HARNESS_MAX_EXTRACTED_BYTES", 1_073_741_824, 1),
            max_poc_bytes=_env_int("HARNESS_MAX_POC_BYTES", 16_777_216, 1),
            max_tool_output_chars=_env_int("HARNESS_MAX_TOOL_OUTPUT_CHARS", 12_000, 256),
            llm_mode=llm_mode,
        )

    def validate_runtime(self) -> None:
        if self.llm_mode == "deepseek" and not self.api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is required in deepseek mode; inject it as an environment variable"
            )

