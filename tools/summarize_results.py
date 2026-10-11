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


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize common AgentWhetters/Custom CSV results")
    parser.add_argument("csv_files", nargs="+", type=Path)
    args = parser.parse_args()
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for path in args.csv_files:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                grouped[row["harness"]].append(row)

    print("harness,tasks,success_rate,avg_tokens,avg_time_seconds,total_cost_usd,cost_per_success_usd")
    for harness in sorted(grouped):
        rows = grouped[harness]
        successes = sum(row.get("status") == "PASS" for row in rows)
        total_cost = sum(number(row, "api_cost_usd") for row in rows)
        avg_tokens = sum(number(row, "total_tokens") for row in rows) / len(rows)
        avg_time = sum(number(row, "time_seconds") for row in rows) / len(rows)
        cost_per_success = total_cost / successes if successes else 0
        print(
            f"{harness},{len(rows)},{successes / len(rows):.6f},{avg_tokens:.2f},"
            f"{avg_time:.3f},{total_cost:.8f},{cost_per_success:.8f}"
        )


if __name__ == "__main__":
    main()

