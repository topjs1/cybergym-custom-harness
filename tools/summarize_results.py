from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path


def number(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, "") or 0)
    except ValueError:
        return 0.0


def _group(csv_files: list[Path]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for path in csv_files:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                grouped[row.get("harness", "unknown")].append(row)
    return grouped


def _success_rate(rows: list[dict[str, str]]) -> float:
    if not rows:
        return 0.0
    return sum(row.get("status") == "PASS" for row in rows) / len(rows)


def print_comparison(grouped: dict[str, list[dict[str, str]]]) -> None:
    print("harness,tasks,success_rate,avg_tokens,avg_time_seconds,total_cost_usd,cost_per_success_usd")
    for harness in sorted(grouped):
        rows = grouped[harness]
        successes = sum(row.get("status") == "PASS" for row in rows)
        total_cost = sum(number(row, "api_cost_usd") for row in rows)
        avg_tokens = sum(number(row, "total_tokens") for row in rows) / len(rows)
        avg_time = sum(number(row, "time_seconds") for row in rows) / len(rows)
        cost_per_success = total_cost / successes if successes else 0
        print(
            f"{harness},{len(rows)},{_success_rate(rows):.6f},{avg_tokens:.2f},"
            f"{avg_time:.3f},{total_cost:.8f},{cost_per_success:.8f}"
        )


def print_submission(rows: list[dict[str, str]], agent_name: str, link: str) -> None:
    """Emit the CyberGym SUBMISSION.md YAML schema using per-task averages."""
    total = len(rows)
    if total == 0:
        print("# no rows")
        return
    models = sorted({row.get("model", "unknown") for row in rows})
    print(f"agent_name: {agent_name}")
    print(f"success_rate: {_success_rate(rows):.4f}")
    print(f"link: {link}")
    print("category: agent")
    print("models:")
    for model in models:
        subset = [row for row in rows if row.get("model") == model]
        count = len(subset)

        def avg(key: str) -> float:
            return sum(number(row, key) for row in subset) / count

        # input_tokens in our CSV is total prompt tokens; cache_miss is the non-cached part.
        print(f"  - name: {model}")
        print(f"    input_tokens: {avg('input_cache_miss_tokens'):.0f}")
        print(f"    cache_read_tokens: {avg('cached_tokens'):.0f}")
        print("    cache_creation_tokens: 0")
        print(f"    output_tokens: {avg('output_tokens'):.0f}")
        print(f"    est_usd_cost: {avg('api_cost_usd'):.6f}")
        print(f"    time_cost_sec: {avg('time_seconds'):.1f}")
        print(f"    llm_requests: {avg('api_calls'):.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize common AgentWhetters/Custom CSV results")
    parser.add_argument("csv_files", nargs="+", type=Path)
    parser.add_argument("--submission", action="store_true", help="Emit the CyberGym SUBMISSION.md YAML schema")
    parser.add_argument("--agent-name", default="cybergym-custom-harness")
    parser.add_argument("--link", default="")
    parser.add_argument("--harness", default="Custom", help="Filter to one harness when using --submission")
    args = parser.parse_args()

    grouped = _group(args.csv_files)
    if args.submission:
        rows = grouped.get(args.harness, [])
        print_submission(rows, args.agent_name, args.link)
    else:
        print_comparison(grouped)


if __name__ == "__main__":
    main()
