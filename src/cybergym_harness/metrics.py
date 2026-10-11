from __future__ import annotations

import asyncio
import csv
import json
import os
import resource
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


CSV_FIELDS = [
    "harness", "task_id", "model", "input_tokens", "output_tokens",
    "cached_tokens", "total_tokens", "api_calls", "time_seconds",
    "api_cost_usd", "peak_memory_mb", "attempts", "status",
    "task_id_source", "input_cache_miss_tokens", "usage_source",
    "api_cost_actual_usd", "api_cost_source", "pricing_tier",
    "pricing_snapshot", "error",
]

PRICING_SNAPSHOT = "DeepSeek official pricing retrieved 2026-10-12"
PRICES_USD_PER_MILLION: dict[str, dict[str, dict[str, float]]] = {
    "deepseek-flash": {
        "off_peak": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60},
        "peak": {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20},
    },
    "deepseek-v4-pro": {
        "off_peak": {"cache_hit": 0.022, "cache_miss": 0.66, "output": 1.98},
        "peak": {"cache_hit": 0.044, "cache_miss": 1.32, "output": 3.96},
    },
}


@dataclass(slots=True)
class UsageRecord:
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cache_miss_tokens: int
    total_tokens: int
    source: str
    latency_seconds: float


@dataclass(slots=True)
class TaskMetrics:
    task_id: str
    task_id_source: str
    model: str
    pricing_tier: str
    start_monotonic: float = field(default_factory=time.monotonic)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_miss_tokens: int = 0
    total_tokens: int = 0
    api_calls: int = 0
    attempts: int = 0
    status: str = "ERROR"
    error: str = ""
    peak_memory_mb: float = 0.0
    usage_sources: set[str] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=list)

    def add_usage(self, usage: UsageRecord) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cached_tokens += usage.cached_tokens
        self.cache_miss_tokens += usage.cache_miss_tokens
        self.total_tokens += usage.total_tokens
        self.api_calls += 1
        self.usage_sources.add(usage.source)
        self.event("llm_usage", **asdict(usage))

    def event(self, event: str, **payload: Any) -> None:
        self.events.append({"at": time.time(), "event": event, **payload})

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_monotonic

    @property
    def usage_source(self) -> str:
        if not self.usage_sources:
            return "none"
        if self.usage_sources == {"api_actual"}:
            return "api_actual"
        if self.usage_sources == {"estimated_missing_api_usage"}:
            return "estimated"
        if self.usage_sources == {"mock"}:
            return "mock"
        return "mixed"

    def estimated_cost(self) -> tuple[str, str]:
        rates = PRICES_USD_PER_MILLION.get(self.model, {}).get(self.pricing_tier)
        if rates is None:
            return "", "unavailable"
        value = (
            self.cached_tokens * rates["cache_hit"]
            + self.cache_miss_tokens * rates["cache_miss"]
            + self.output_tokens * rates["output"]
        ) / 1_000_000
        return f"{value:.8f}", "estimated_from_token_usage"

    def csv_row(self) -> dict[str, str | int | float]:
        estimated_cost, cost_source = self.estimated_cost()
        return {
            "harness": "Custom",
            "task_id": self.task_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "api_calls": self.api_calls,
            "time_seconds": f"{self.elapsed:.3f}",
            "api_cost_usd": estimated_cost,
            "peak_memory_mb": f"{self.peak_memory_mb:.3f}",
            "attempts": self.attempts,
            "status": self.status,
            "task_id_source": self.task_id_source,
            "input_cache_miss_tokens": self.cache_miss_tokens,
            "usage_source": self.usage_source,
            "api_cost_actual_usd": "",
            "api_cost_source": cost_source,
            "pricing_tier": self.pricing_tier,
            "pricing_snapshot": PRICING_SNAPSHOT if cost_source != "unavailable" else "",
            "error": self.error[:1000],
        }


def _current_rss_mb() -> float:
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text(errors="replace").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return value / (1024 * 1024)
    return value / 1024


class MemorySampler:
    def __init__(self, metrics: TaskMetrics):
        self.metrics = metrics
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "MemorySampler":
        self.metrics.peak_memory_mb = _current_rss_mb()
        self._task = asyncio.create_task(self._sample())
        return self

    async def _sample(self) -> None:
        while not self._stop.is_set():
            self.metrics.peak_memory_mb = max(self.metrics.peak_memory_mb, _current_rss_mb())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.25)
            except TimeoutError:
                pass

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._stop.set()
        if self._task:
            await self._task
        self.metrics.peak_memory_mb = max(self.metrics.peak_memory_mb, _current_rss_mb())


class MetricsWriter:
    def __init__(self, csv_path: Path, jsonl_path: Path):
        self.csv_path = csv_path
        self.jsonl_path = jsonl_path
        self._lock = asyncio.Lock()

    async def write(self, metrics: TaskMetrics) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_sync, metrics)

    def _write_sync(self, metrics: TaskMetrics) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        with self.csv_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(metrics.csv_row())
            handle.flush()
            os.fsync(handle.fileno())
        record = {"summary": metrics.csv_row(), "events": metrics.events}
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def estimate_usage(payload: Any, response_message: Any) -> UsageRecord:
    input_chars = len(json.dumps(payload, ensure_ascii=False, default=str))
    output_chars = len(json.dumps(response_message, ensure_ascii=False, default=str))
    input_tokens = max(1, (input_chars + 3) // 4)
    output_tokens = max(1, (output_chars + 3) // 4)
    return UsageRecord(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=0,
        cache_miss_tokens=input_tokens,
        total_tokens=input_tokens + output_tokens,
        source="estimated_missing_api_usage",
        latency_seconds=0.0,
    )

