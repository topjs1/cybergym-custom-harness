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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


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
    dynamic_analysis: str = "off"
    dynamic_url: str = "http://dynamic-sandbox:9110"
    dynamic_timeout_seconds: int = 120
    build_timeout_seconds: int = 900
    fuzz_seconds: int = 60
    fuzz_engine: str = "libfuzzer"
    max_crashes: int = 8
    builder_image: str = "gcr.io/oss-fuzz-base/base-builder"
    runner_image: str = "gcr.io/oss-fuzz-base/base-runner"
    build_allow_network: bool = False
    build_command: str = "bash build.sh"
    dynamic_host_root: Path | None = None
    sidecar_token: str = ""
    recon_subagent: bool = False
    reviewer_subagent: bool = False
    recon_max_turns: int = 6
    subagent_output_tokens: int = 1024
    reviewer_max_calls: int = 2
    stall_threshold: int = 3
    mutation_attempts: int = 0
    max_resume_nudges: int = 4
    force_submit_after: int = 3
    tracks: int = 1
    max_cost_usd: float = 0.0

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
        dynamic_analysis = os.getenv("HARNESS_DYNAMIC_ANALYSIS", "off").strip().lower()
        if dynamic_analysis not in {"off", "sidecar", "inline"}:
            raise ValueError("HARNESS_DYNAMIC_ANALYSIS must be off, sidecar, or inline")
        fuzz_engine = os.getenv("HARNESS_FUZZ_ENGINE", "libfuzzer").strip().lower()
        if fuzz_engine not in {"libfuzzer", "afl"}:
            raise ValueError("HARNESS_FUZZ_ENGINE must be libfuzzer or afl")
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
            dynamic_analysis=dynamic_analysis,
            dynamic_url=os.getenv("HARNESS_DYNAMIC_URL", "http://dynamic-sandbox:9110").rstrip("/"),
            dynamic_timeout_seconds=_env_int("HARNESS_DYNAMIC_TIMEOUT_SECONDS", 120, 1),
            build_timeout_seconds=_env_int("HARNESS_BUILD_TIMEOUT_SECONDS", 900, 1),
            fuzz_seconds=_env_int("HARNESS_FUZZ_SECONDS", 60, 1),
            fuzz_engine=fuzz_engine,
            max_crashes=_env_int("HARNESS_MAX_CRASHES", 8, 1),
            builder_image=os.getenv("HARNESS_BUILDER_IMAGE", "gcr.io/oss-fuzz-base/base-builder").strip(),
            runner_image=os.getenv("HARNESS_RUNNER_IMAGE", "gcr.io/oss-fuzz-base/base-runner").strip(),
            build_allow_network=_env_bool("HARNESS_BUILD_ALLOW_NETWORK", False),
            build_command=os.getenv("HARNESS_BUILD_COMMAND", "bash build.sh").strip(),
            dynamic_host_root=(
                Path(host_root) if (host_root := os.getenv("HARNESS_DYNAMIC_HOST_ROOT", "").strip()) else None
            ),
            sidecar_token=os.getenv("HARNESS_SIDECAR_TOKEN", "").strip(),
            recon_subagent=_env_bool("HARNESS_RECON_SUBAGENT", False),
            reviewer_subagent=_env_bool("HARNESS_REVIEWER_SUBAGENT", False),
            recon_max_turns=_env_int("HARNESS_RECON_MAX_TURNS", 6, 1),
            subagent_output_tokens=_env_int("HARNESS_SUBAGENT_OUTPUT_TOKENS", 1024, 1),
            reviewer_max_calls=_env_int("HARNESS_REVIEWER_MAX_CALLS", 2, 0),
            stall_threshold=_env_int("HARNESS_STALL_THRESHOLD", 3, 1),
            mutation_attempts=_env_int("HARNESS_MUTATION_ATTEMPTS", 0, 0),
            max_resume_nudges=_env_int("HARNESS_RESUME_NUDGES", 4, 0),
            force_submit_after=_env_int("HARNESS_FORCE_SUBMIT_AFTER", 6, 1),
            tracks=_env_int("HARNESS_TRACKS", 1, 1),
            max_cost_usd=_env_float("HARNESS_MAX_COST_USD", 0.0, 0.0),
        )

    def validate_runtime(self) -> None:
        if self.llm_mode == "deepseek" and not self.api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is required in deepseek mode; inject it as an environment variable"
            )

