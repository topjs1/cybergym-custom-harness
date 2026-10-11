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


def test_builder_long_structured_ops(tmp_path):
    path = build_poc([
        {"type": "u8", "value": 0x7F},
        {"type": "repeat_seq", "value": "de ad", "encoding": "hex", "count": 3},
        {"type": "pad", "byte": 0x00, "to": 16},
        {"type": "crc32", "endian": "little"},
    ], tmp_path, 1024)
    data = path.read_bytes()
    assert data[0] == 0x7F
    assert data[1:7] == bytes.fromhex("dead") * 3
    assert len(data) == 20
    assert data[7:16] == b"\x00" * 9


def test_builder_patch_overwrites_offset(tmp_path):
    path = build_poc([
        {"type": "hex", "value": "00000000"},
        {"type": "text", "value": "PAYLOAD"},
        {"type": "patch", "offset": 0, "value": "0102", "encoding": "hex"},
    ], tmp_path, 1024)
    assert path.read_bytes() == b"\x01\x02\x00\x00PAYLOAD"


def test_builder_pad_and_patch_bounds_are_checked(tmp_path):
    with pytest.raises(ValueError):
        build_poc([{"type": "hex", "value": "4142"}, {"type": "pad", "to": 1}], tmp_path, 100)
    with pytest.raises(ValueError):
        build_poc([{"type": "hex", "value": "41"}, {"type": "patch", "offset": 5, "value": "42"}], tmp_path, 100)


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

