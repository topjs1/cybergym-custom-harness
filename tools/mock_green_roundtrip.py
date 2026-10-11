from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import os
import socket
import subprocess
import sys
import time
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
from tests.helpers import challenge_message  # noqa: E402
from cybergym_harness.config import Settings  # noqa: E402
from cybergym_harness.server import build_application  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def data_part(message: Message) -> dict | None:
    for wrapped in message.parts:
        if isinstance(wrapped.root, DataPart):
            return wrapped.root.data
    return None


def file_bytes(message: Message) -> bytes | None:
    for wrapped in message.parts:
        if isinstance(wrapped.root, FilePart) and isinstance(wrapped.root.file, FileWithBytes):
            return base64.b64decode(wrapped.root.file.bytes)
    return None


async def wait_ready(url: str) -> None:
    async with httpx.AsyncClient(timeout=1) as client:
        for _ in range(100):
            try:
                response = await client.get(url + "/.well-known/agent-card.json")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError("Purple A2A server did not become ready")


async def wait_ready_client(http: httpx.AsyncClient, url: str) -> None:
    for _ in range(100):
        try:
            response = await http.get(url + "/.well-known/agent-card.json")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("Purple A2A server did not become ready")


async def run_roundtrip(http: httpx.AsyncClient, url: str) -> tuple[int, bytes | None]:
    attempts = 0
    final_poc: bytes | None = None
    card = await A2ACardResolver(httpx_client=http, base_url=url).get_agent_card()
    client = ClientFactory(ClientConfig(httpx_client=http, streaming=True)).create(card)
    async for event in client.send_message(challenge_message("mock:network-roundtrip")):
        match event:
            case (task, TaskStatusUpdateEvent() as update) if update.status.message:
                action = data_part(update.status.message)
                if action and action.get("action") == "test_vulnerable":
                    attempts += 1
                    candidate = file_bytes(update.status.message)
                    result = {
                        "exit_code": 1 if candidate == b"mock-crash-poc" else 0,
                        "output": f"mock verifier attempt {attempts}",
                    }
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
    return attempts, final_poc


def configure_paths() -> tuple[Path, Path]:
    results = PROJECT / "data" / "test-results.csv"
    events = PROJECT / "data" / "test-events.jsonl"
    results.unlink(missing_ok=True)
    events.unlink(missing_ok=True)
    os.environ.update({
        "HARNESS_LLM_MODE": "mock",
        "HARNESS_PYTHON_BACKEND": "disabled",
        "HARNESS_RESULTS_CSV": str(results),
        "HARNESS_EVENTS_JSONL": str(events),
        "HARNESS_WORK_ROOT": str(PROJECT / "data" / "tasks"),
        "HARNESS_FEEDBACK_TIMEOUT_SECONDS": "10",
        "HARNESS_TASK_TIMEOUT_SECONDS": "30",
    })
    return results, events


async def run_inprocess() -> int:
    results, _ = configure_paths()
    url = "http://purple.test"
    app = build_application(Settings.from_env(), url)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=url, timeout=30) as http:
        attempts, poc = await run_roundtrip(http, url)
    rows = list(csv.DictReader(results.open(newline=""))) if results.is_file() else []
    passed = len(rows) == 1 and rows[0]["status"] == "PASS" and rows[0]["attempts"] == "2"
    if attempts != 2 or poc != b"mock-crash-poc" or not passed:
        raise RuntimeError(f"unexpected roundtrip: attempts={attempts}, poc={poc!r}, results={results.exists()}")
    print(f"PASS a2a_asgi_roundtrip attempts={attempts} poc_bytes={len(poc)} results={results}")
    return 0


async def run_tcp(keep_logs: bool) -> int:
    results, _ = configure_paths()
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(PROJECT / "src"),
    })
    process = subprocess.Popen(
        [sys.executable, "-m", "cybergym_harness.server", "--host", "127.0.0.1", "--port", str(port), "--card-url", url],
        cwd=PROJECT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        await wait_ready(url)
        async with httpx.AsyncClient(timeout=30) as http:
            attempts, poc = await run_roundtrip(http, url)
        rows = list(csv.DictReader(results.open(newline=""))) if results.is_file() else []
        passed = len(rows) == 1 and rows[0]["status"] == "PASS" and rows[0]["attempts"] == "2"
        if attempts != 2 or poc != b"mock-crash-poc" or not passed:
            raise RuntimeError(f"unexpected roundtrip: attempts={attempts}, poc={poc!r}, results={results.exists()}")
        print(f"PASS a2a_roundtrip attempts={attempts} poc_bytes={len(poc)} results={results}")
        return 0
    finally:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        if keep_logs or process.returncode not in {0, -15}:
            print(output)


async def run_external(url: str) -> int:
    async with httpx.AsyncClient(timeout=30) as http:
        await wait_ready(url)
        attempts, poc = await run_roundtrip(http, url)
    if attempts != 2 or poc != b"mock-crash-poc":
        raise RuntimeError(f"unexpected external roundtrip: attempts={attempts}, poc={poc!r}")
    print(f"PASS external_a2a_roundtrip attempts={attempts} poc_bytes={len(poc)} url={url}")
    return 0


async def run_uds(keep_logs: bool) -> int:
    results, _ = configure_paths()
    url = "http://purple.test"
    socket_path = PROJECT / "data" / "mock-purple.sock"
    socket_path.unlink(missing_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT / "src")
    process = subprocess.Popen(
        [sys.executable, "-m", "cybergym_harness.server", "--uds", str(socket_path), "--card-url", url],
        cwd=PROJECT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
        async with httpx.AsyncClient(transport=transport, timeout=30) as http:
            await wait_ready_client(http, url)
            attempts, poc = await run_roundtrip(http, url)
        rows = list(csv.DictReader(results.open(newline=""))) if results.is_file() else []
        passed = len(rows) == 1 and rows[0]["status"] == "PASS" and rows[0]["attempts"] == "2"
        if attempts != 2 or poc != b"mock-crash-poc" or not passed:
            raise RuntimeError(f"unexpected roundtrip: attempts={attempts}, poc={poc!r}, rows={rows}")
        print(f"PASS a2a_uds_roundtrip attempts={attempts} poc_bytes={len(poc)} results={results}")
        return 0
    finally:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        socket_path.unlink(missing_ok=True)
        if keep_logs or process.returncode not in {0, -15}:
            print(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-server-log", action="store_true")
    parser.add_argument("--tcp", action="store_true", help="Use a real TCP listener instead of in-process ASGI")
    parser.add_argument("--inprocess", action="store_true", help="Use ASGI transport (cannot exercise live feedback streaming)")
    parser.add_argument("--url", help="Exercise an already-running Purple Agent, such as a Docker container")
    args = parser.parse_args()
    if args.url:
        target = run_external(args.url.rstrip("/"))
    elif args.tcp:
        target = run_tcp(args.keep_server_log)
    elif args.inprocess:
        target = run_inprocess()
    else:
        target = run_uds(args.keep_server_log)
    raise SystemExit(asyncio.run(target))


if __name__ == "__main__":
    main()
