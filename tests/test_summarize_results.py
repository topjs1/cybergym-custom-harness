from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]

HEADER = [
    "harness", "task_id", "model", "input_tokens", "output_tokens", "cached_tokens",
    "total_tokens", "api_calls", "time_seconds", "api_cost_usd", "peak_memory_mb",
    "attempts", "status", "task_id_source", "input_cache_miss_tokens", "usage_source",
    "api_cost_actual_usd", "api_cost_source", "pricing_tier", "pricing_snapshot", "error",
    "role_tokens_json", "phase_seconds_json",
]


def _load():
    spec = importlib.util.spec_from_file_location("summarize_results", PROJECT / "tools" / "summarize_results.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["summarize_results"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


summary = _load()


def _write_csv(path: Path) -> None:
    rows = [
        {"harness": "Custom", "model": "deepseek-flash", "input_cache_miss_tokens": "1000",
         "cached_tokens": "4000", "output_tokens": "500", "api_calls": "5",
         "time_seconds": "10.0", "api_cost_usd": "0.001", "total_tokens": "5000", "status": "PASS"},
        {"harness": "Custom", "model": "deepseek-flash", "input_cache_miss_tokens": "3000",
         "cached_tokens": "0", "output_tokens": "100", "api_calls": "3",
         "time_seconds": "20.0", "api_cost_usd": "0.003", "total_tokens": "3100", "status": "FAIL"},
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in HEADER})


def test_submission_schema_aggregates(tmp_path: Path, capsys):
    path = tmp_path / "results.csv"
    _write_csv(path)
    grouped = summary._group([path])
    summary.print_submission(grouped["Custom"], "my-agent", "https://x")
    out = capsys.readouterr().out
    assert "agent_name: my-agent" in out
    assert "success_rate: 0.5000" in out
    assert "cache_read_tokens: 2000" in out
    assert "input_tokens: 2000" in out
    assert "llm_requests: 4.0" in out


def test_comparison_csv(tmp_path: Path, capsys):
    path = tmp_path / "results.csv"
    _write_csv(path)
    summary.print_comparison(summary._group([path]))
    out = capsys.readouterr().out
    assert "harness,tasks,success_rate" in out
    assert "Custom,2,0.500000" in out
