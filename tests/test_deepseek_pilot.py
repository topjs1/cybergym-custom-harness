from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from a2a.types import DataPart, FilePart, FileWithBytes

PROJECT = Path(__file__).resolve().parents[1]


def _load_pilot():
    spec = importlib.util.spec_from_file_location("deepseek_pilot", PROJECT / "tools" / "deepseek_pilot.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["deepseek_pilot"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pilot = _load_pilot()


def test_parse_stub_variants():
    assert pilot.parse_stub("never") == ("never", 0)
    assert pilot.parse_stub("always") == ("always", 0)
    assert pilot.parse_stub("nth=3") == ("nth", 3)
    for bad in ("", "sometimes", "nth=0", "nth=x"):
        with pytest.raises(ValueError):
            pilot.parse_stub(bad)


def test_stub_verifier_nth():
    verifier = pilot.StubVerifier("nth=2")
    assert verifier.verdict(b"a")["exit_code"] == 0
    assert verifier.verdict(b"b")["exit_code"] == 1
    assert verifier.submissions == 2


def test_stub_verifier_always_and_never():
    assert pilot.StubVerifier("always").verdict(b"x")["exit_code"] == 1
    assert pilot.StubVerifier("never").verdict(b"x")["exit_code"] == 0


def test_challenge_from_dir_includes_files_and_task_id(tmp_path: Path):
    (tmp_path / "repo-vul.tar.gz").write_bytes(b"archive")
    (tmp_path / "description.txt").write_bytes(b"heap overflow")
    message = pilot.challenge_from_dir(tmp_path, "arvo:10400")
    names = [
        wrapped.root.file.name
        for wrapped in message.parts
        if isinstance(wrapped.root, FilePart) and isinstance(wrapped.root.file, FileWithBytes)
    ]
    assert "repo-vul.tar.gz" in names and "description.txt" in names
    task_id_parts = [
        wrapped.root.data for wrapped in message.parts if isinstance(wrapped.root, DataPart)
    ]
    assert {"task_id": "arvo:10400"} in task_id_parts


def test_challenge_from_dir_rejects_empty(tmp_path: Path):
    with pytest.raises(ValueError):
        pilot.challenge_from_dir(tmp_path, "x")


def test_cost_ceiling_reports_estimate():
    assert "$" in pilot._cost_ceiling("deepseek-flash", 100_000)
    assert "unknown" in pilot._cost_ceiling("no-such-model", 100_000)


@pytest.mark.asyncio
async def test_pilot_roundtrip_drives_harness_over_tcp(tmp_path: Path):
    import os
    import socket
    import subprocess

    import httpx
    from tests.helpers import challenge_message

    env = os.environ.copy()
    env.update({
        "HARNESS_LLM_MODE": "mock",
        "HARNESS_PYTHON_BACKEND": "disabled",
        "HARNESS_DYNAMIC_ANALYSIS": "off",
        "HARNESS_RESULTS_CSV": str(tmp_path / "r.csv"),
        "HARNESS_EVENTS_JSONL": str(tmp_path / "e.jsonl"),
        "HARNESS_WORK_ROOT": str(tmp_path / "tasks"),
        "HARNESS_FEEDBACK_TIMEOUT_SECONDS": "8",
        "HARNESS_TASK_TIMEOUT_SECONDS": "20",
        "PYTHONPATH": str(PROJECT / "src"),
    })
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [sys.executable, "-m", "cybergym_harness.server", "--host", "127.0.0.1",
         "--port", str(port), "--card-url", url + "/"],
        cwd=PROJECT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            await pilot._wait_ready(http, url)
            result = await pilot.run_roundtrip(
                http, url, challenge_message("pilot:tcp"), pilot.StubVerifier("nth=2")
            )
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
    assert result.attempts == 2
    assert result.final_poc == b"mock-crash-poc"
