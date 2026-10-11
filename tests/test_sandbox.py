from __future__ import annotations

from pathlib import Path

import pytest

import cybergym_harness.sandbox as sandbox_module
from cybergym_harness.sandbox import DockerPythonSandbox, build_poc


def test_structured_builder(tmp_path):
    path = build_poc([
        {"type": "hex", "value": "4142"},
        {"type": "u16", "value": 0x1234, "endian": "little"},
        {"type": "repeat", "byte": 0x43, "count": 3},
    ], tmp_path, 100)
    assert path.read_bytes() == b"AB\x34\x12CCC"


@pytest.mark.asyncio
async def test_docker_python_command_is_hardened(monkeypatch, tmp_path):
    captured: list[str] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

        def kill(self):
            self.returncode = -9

    async def fake_create(*args, **kwargs):
        captured.extend(str(item) for item in args)
        mount = captured[captured.index("--mount") + 1]
        source = mount.split("src=", 1)[1].split(",dst=", 1)[0]
        Path(source, "poc").write_bytes(b"sandboxed")
        return FakeProcess()

    monkeypatch.setattr(sandbox_module.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", fake_create)
    runner = DockerPythonSandbox("python:3.13.9-slim-bookworm", 5, 1024)
    result = await runner.execute("open('/output/poc','wb').write(b'x')", tmp_path)
    assert result.ok
    for required in ["--network=none", "--read-only", "--cap-drop=ALL", "--user=65534:65534"]:
        assert required in captured
    assert "DEEPSEEK_API_KEY" not in " ".join(captured)

