"""A1a DeepSeek pilot: drive the Purple harness with real DeepSeek and a stub verifier.

This validates the end-to-end pipeline (DeepSeek thinking + tool calls, static triage,
build_poc, submit_poc, feedback loop, budgets, metrics, final artifact) WITHOUT
downloading CyberGym target images or running the real Green. The stub verifier stands in
for the Green test_vulnerable round trip.

Usage:
    read -s DEEPSEEK_API_KEY && export DEEPSEEK_API_KEY
    uv run python tools/deepseek_pilot.py --tcp --stub nth=3
    unset DEEPSEEK_API_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import (
    DataPart,
    FilePart,
    FileWithBytes,
    Message,
    Part,
    Role,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "src"))

from cybergym_harness.config import Settings  # noqa: E402
from cybergym_harness.metrics import PRICES_USD_PER_MILLION  # noqa: E402
from cybergym_harness.server import build_application  # noqa: E402
from tests.helpers import challenge_message  # noqa: E402

TASK_FILES = (
    "repo-vul.tar.gz",
    "repo-fix.tar.gz",
    "description.txt",
    "error.txt",
    "patch.diff",
    "README.md",
)


@dataclass(slots=True)
class RoundtripResult:
    attempts: int
    final_poc: bytes | None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def parse_stub(spec: str) -> tuple[str, int]:
    spec = (spec or "").strip().lower()
    if spec in {"never", "always"}:
        return spec, 0
    if spec.startswith("nth="):
        try:
            value = int(spec.split("=", 1)[1])
        except ValueError as exc:
            raise ValueError(f"invalid --stub value: {spec!r}") from exc
        if value < 1:
            raise ValueError("--stub nth must be >= 1")
        return "nth", value
    raise ValueError(f"unknown --stub value: {spec!r} (use never, always, or nth=N)")


class StubVerifier:
    """Deterministic stand-in for the Green test_vulnerable round trip."""

    def __init__(self, spec: str, canned_output: str = ""):
        self.kind, self.nth = parse_stub(spec)
        self.canned_output = canned_output
        self.submissions = 0

    def verdict(self, poc: bytes) -> dict:
        self.submissions += 1
        if self.kind == "always":
            crash = True
        elif self.kind == "nth":
            crash = self.submissions == self.nth
        else:
            crash = False
        output = self.canned_output or f"stub verifier submission {self.submissions} ({len(poc)} bytes)"
        return {
            "exit_code": 1 if crash else 0,
            "output": output if crash else output + "\n*** no crash ***",
        }


def challenge_from_dir(task_dir: Path, task_id: str) -> Message:
    parts: list[Part] = [
        Part(root=DataPart(data={"task_id": task_id})),
    ]
    names: list[str] = []
    for name in TASK_FILES:
        path = task_dir / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        parts.append(Part(root=FilePart(file=FileWithBytes(
            bytes=base64.b64encode(raw).decode("ascii"),
            name=name,
            mime_type="application/octet-stream",
        ))))
        names.append(name)
    if not names:
        raise ValueError(f"no challenge files found in {task_dir}")
    parts.append(Part(root=DataPart(data={"attachments": names})))
    return Message(kind="message", role=Role.user, parts=parts, message_id=uuid4().hex)


def _data_part(message: Message) -> dict | None:
    for wrapped in message.parts:
        if isinstance(wrapped.root, DataPart):
            return wrapped.root.data
    return None


def _file_bytes(message: Message) -> bytes | None:
    for wrapped in message.parts:
        if isinstance(wrapped.root, FilePart) and isinstance(wrapped.root.file, FileWithBytes):
            return base64.b64decode(wrapped.root.file.bytes)
    return None


async def _wait_ready(http: httpx.AsyncClient, url: str) -> None:
    for _ in range(200):
        try:
            response = await http.get(url + "/.well-known/agent-card.json")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("Purple A2A server did not become ready")


async def run_roundtrip(
    http: httpx.AsyncClient,
    url: str,
    challenge: Message,
    verifier: StubVerifier,
) -> RoundtripResult:
    final_poc: bytes | None = None
    attempts = 0
    card = await A2ACardResolver(httpx_client=http, base_url=url).get_agent_card()
    client = ClientFactory(ClientConfig(httpx_client=http, streaming=True)).create(card)
    async for event in client.send_message(challenge):
        match event:
            case (task, TaskStatusUpdateEvent() as update) if update.status.message:
                action = _data_part(update.status.message)
                if action and action.get("action") == "test_vulnerable":
                    candidate = _file_bytes(update.status.message) or b""
                    attempts += 1
                    result = verifier.verdict(candidate)
                    reply = Message(
                        kind="message",
                        role=Role.user,
                        parts=[Part(root=DataPart(data=result))],
                        message_id=uuid4().hex,
                        context_id=task.context_id,
                    )
                    async for _ in client.send_message(reply):
                        pass
            case (task, TaskArtifactUpdateEvent()):
                for artifact in task.artifacts or []:
                    for wrapped in artifact.parts:
                        if isinstance(wrapped.root, FilePart) and isinstance(wrapped.root.file, FileWithBytes):
                            final_poc = base64.b64decode(wrapped.root.file.bytes)
            case _:
                pass
    return RoundtripResult(attempts=attempts, final_poc=final_poc)


def _pilot_env(args: argparse.Namespace, out_dir: Path) -> dict[str, str]:
    return {
        "HARNESS_LLM_MODE": "deepseek",
        "DEEPSEEK_MODEL": args.model,
        "DEEPSEEK_THINKING": args.thinking,
        "DEEPSEEK_MAX_OUTPUT_TOKENS": str(args.max_output_tokens),
        "HARNESS_PYTHON_BACKEND": "disabled",
        "HARNESS_DYNAMIC_ANALYSIS": "off",
        "HARNESS_TOKEN_BUDGET": str(args.token_budget),
        "HARNESS_MAX_ATTEMPTS": str(args.max_attempts),
        "HARNESS_TASK_TIMEOUT_SECONDS": str(args.task_timeout),
        "HARNESS_FEEDBACK_TIMEOUT_SECONDS": "30",
        "HARNESS_TRACKS": str(args.tracks),
        "HARNESS_MAX_COST_USD": str(args.cost_cap_usd),
        "HARNESS_RESUME_NUDGES": str(args.resume_nudges),
        "HARNESS_MUTATION_ATTEMPTS": str(args.mutation_attempts),
        "HARNESS_RESULTS_CSV": str(out_dir / "results.csv"),
        "HARNESS_EVENTS_JSONL": str(out_dir / "events.jsonl"),
        "HARNESS_WORK_ROOT": str(out_dir / "tasks"),
    }


def _cost_ceiling(model: str, token_budget: int) -> str:
    rates = PRICES_USD_PER_MILLION.get(model, {}).get("off_peak")
    if not rates:
        return "unknown (model not in pricing table)"
    value = token_budget * (rates["cache_miss"] + rates["output"]) / 1_000_000
    return f"~${value:.3f} worst-case (all tokens as cache-miss input + output)"


def _summarize(results_csv: Path) -> dict[str, str]:
    if not results_csv.is_file():
        return {}
    with results_csv.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows[-1] if rows else {}


async def _run(args: argparse.Namespace, out_dir: Path) -> int:
    challenge = challenge_from_dir(Path(args.task_dir), args.task_id) if args.task_dir else challenge_message("pilot:0001")
    verifier = StubVerifier(args.stub, _read_canned(args.stub_output))
    url = f"http://127.0.0.1:{free_port()}"
    results_csv = out_dir / "results.csv"

    process: subprocess.Popen | None = None
    try:
        if args.inprocess:
            app = build_application(Settings.from_env(), url)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=url, timeout=args.task_timeout + 30) as http:
                result = await run_roundtrip(http, url, challenge, verifier)
        else:
            env = os.environ.copy()
            env.update(_pilot_env(args, out_dir))
            env["PYTHONPATH"] = str(PROJECT / "src")
            process = subprocess.Popen(
                [sys.executable, "-m", "cybergym_harness.server", "--host", "127.0.0.1",
                 "--port", str(url.rsplit(":", 1)[1]), "--card-url", url + "/"],
                cwd=PROJECT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            async with httpx.AsyncClient(timeout=args.task_timeout + 30) as http:
                await _wait_ready(http, url)
                result = await run_roundtrip(http, url, challenge, verifier)
    finally:
        if process is not None:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
            if args.keep_server_log:
                print(output)

    row = _summarize(results_csv)
    print("\n=== DeepSeek pilot summary ===")
    print(f"out_dir:      {out_dir}")
    print(f"results_csv:  {results_csv}")
    print(f"submissions:  {verifier.submissions} (accepted={result.attempts})")
    print(f"status:       {row.get('status', 'unknown')}")
    print(f"attempts:     {row.get('attempts', '?')}")
    print(f"tokens:       input={row.get('input_tokens', '?')} output={row.get('output_tokens', '?')} cached={row.get('cached_tokens', '?')} total={row.get('total_tokens', '?')}")
    print(f"api_calls:    {row.get('api_calls', '?')}")
    print(f"time_seconds: {row.get('time_seconds', '?')}")
    print(f"est_usd:      {row.get('api_cost_usd', '?')} ({row.get('api_cost_source', '?')})")
    print(f"role_tokens:  {row.get('role_tokens_json', '?')}")
    print(f"final_poc:    {len(result.final_poc) if result.final_poc else 0} bytes")
    if row.get("error"):
        print(f"error:        {row['error']}")
    solved = row.get("status") == "PASS"
    print(f"verdict:      {'PASS (stub accepted a candidate)' if solved else 'FAIL/INCOMPLETE'}")
    if args.require_success and not solved:
        return 2
    return 0


def _read_canned(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(errors="replace")[:8000]


def main() -> None:
    parser = argparse.ArgumentParser(description="A1a DeepSeek pilot for the CyberGym Purple harness")
    parser.add_argument("--task-dir", help="Directory with repo-vul.tar.gz/description.txt/error.txt; omit for a synthetic challenge")
    parser.add_argument("--task-id", default="pilot:0001")
    parser.add_argument("--stub", default="nth=3", help="never | always | nth=N (accept the Nth submission)")
    parser.add_argument("--stub-output", help="Optional canned verifier output (e.g. an ASan log) to feed back")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="enabled", choices=["enabled", "disabled"])
    parser.add_argument("--token-budget", type=int, default=100_000)
    parser.add_argument("--max-attempts", type=int, default=30)
    parser.add_argument("--task-timeout", type=int, default=1800)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--tracks", type=int, default=1)
    parser.add_argument("--resume-nudges", type=int, default=4)
    parser.add_argument("--mutation-attempts", type=int, default=0)
    parser.add_argument("--cost-cap-usd", type=float, default=3.0, help="Hard USD cap; the harness stops once the estimated cost reaches it (0 disables)")
    parser.add_argument("--tcp", action="store_true", help="Use a real TCP subprocess (default)")
    parser.add_argument("--inprocess", action="store_true", help="Use ASGI transport instead of a TCP subprocess")
    parser.add_argument("--keep-server-log", action="store_true")
    parser.add_argument("--require-success", action="store_true")
    args = parser.parse_args()

    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        raise SystemExit("DEEPSEEK_API_KEY is not set. Inject it in the shell; never write it to a file.")

    out_dir = PROJECT / "data" / f"pilot-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    ceiling = _cost_ceiling(args.model, args.token_budget)
    print("=== DeepSeek pilot config ===")
    print(f"model={args.model} thinking={args.thinking} tracks={args.tracks} stub={args.stub}")
    print(f"token_budget={args.token_budget} max_attempts={args.max_attempts} task_timeout={args.task_timeout}s")
    print(f"cost ceiling: {ceiling} (hard cap=${args.cost_cap_usd} unless 0)")
    print(f"task_dir={args.task_dir or '(synthetic challenge)'}")

    for key, value in _pilot_env(args, out_dir).items():
        os.environ[key] = value
    raise SystemExit(asyncio.run(_run(args, out_dir)))


if __name__ == "__main__":
    main()
