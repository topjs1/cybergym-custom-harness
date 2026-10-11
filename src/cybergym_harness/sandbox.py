from __future__ import annotations

import asyncio
import base64
import os
import shutil
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SandboxResult:
    ok: bool
    output: str
    poc_path: Path | None = None


class PythonSandbox:
    async def execute(self, code: str, output_dir: Path) -> SandboxResult:
        raise NotImplementedError


class DisabledPythonSandbox(PythonSandbox):
    async def execute(self, code: str, output_dir: Path) -> SandboxResult:
        return SandboxResult(
            False,
            "Python execution is disabled. Use build_poc, or configure HARNESS_PYTHON_BACKEND=docker.",
        )


class MockPythonSandbox(PythonSandbox):
    """Test-only backend; it never interprets model-supplied Python."""

    async def execute(self, code: str, output_dir: Path) -> SandboxResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "poc"
        path.write_bytes(b"mock-python-poc")
        return SandboxResult(True, "mock sandbox wrote 15 bytes", path)


class DockerPythonSandbox(PythonSandbox):
    def __init__(self, image: str, timeout_seconds: int, max_poc_bytes: int):
        self.image = image
        self.timeout_seconds = timeout_seconds
        self.max_poc_bytes = max_poc_bytes

    async def execute(self, code: str, output_dir: Path) -> SandboxResult:
        docker = shutil.which("docker")
        if not docker:
            return SandboxResult(False, "docker CLI is unavailable; sandbox failed closed")
        output_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(output_dir, 0o777)
        poc_path = output_dir / "poc"
        poc_path.unlink(missing_ok=True)
        command = [
            docker, "run", "--rm", "--pull=never",
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=64",
            "--memory=128m", "--memory-swap=128m", "--cpus=0.5",
            "--user=65534:65534",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m",
            "--mount", f"type=bind,src={output_dir.resolve()},dst=/output",
            "--env", "PYTHONHASHSEED=0",
            self.image,
            "python", "-I", "-S", "-B", "-c", code,
        ]
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "")},
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), self.timeout_seconds)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return SandboxResult(False, f"sandbox timed out after {self.timeout_seconds}s")
        rendered = stdout.decode(errors="replace")
        if stderr:
            rendered += "\n[stderr]\n" + stderr.decode(errors="replace")
        rendered += f"\n[exit_code={proc.returncode}]"
        if proc.returncode != 0:
            return SandboxResult(False, rendered[-8000:])
        if not poc_path.is_file():
            return SandboxResult(False, rendered[-8000:] + "\n/output/poc was not created")
        size = poc_path.stat().st_size
        if size > self.max_poc_bytes:
            poc_path.unlink(missing_ok=True)
            return SandboxResult(False, f"PoC exceeds limit: {size} > {self.max_poc_bytes}")
        return SandboxResult(True, rendered[-8000:] + f"\n[poc_bytes={size}]", poc_path)


def _decode_sequence(value: Any, encoding: str) -> bytes:
    text = str(value or "")
    if encoding == "hex":
        return bytes.fromhex(text)
    if encoding == "base64":
        return base64.b64decode(text, validate=True)
    if encoding == "text":
        return text.encode("utf-8")
    raise ValueError(f"unsupported sequence encoding {encoding!r}")


def build_poc(operations: list[dict[str, Any]], output_dir: Path, max_poc_bytes: int) -> Path:
    """Build bytes with a small non-executable DSL and write generated/poc."""
    if len(operations) > 1000:
        raise ValueError("too many build operations")
    result = bytearray()
    for index, operation in enumerate(operations):
        kind = operation.get("type")
        if kind == "hex":
            result.extend(bytes.fromhex(str(operation.get("value", ""))))
        elif kind == "base64":
            result.extend(base64.b64decode(str(operation.get("value", "")), validate=True))
        elif kind == "text":
            encoding = str(operation.get("encoding", "utf-8"))
            if encoding not in {"utf-8", "ascii", "latin-1"}:
                raise ValueError(f"operation {index}: unsupported encoding")
            result.extend(str(operation.get("value", "")).encode(encoding))
        elif kind == "repeat":
            byte = int(operation.get("byte", 0))
            count = int(operation.get("count", 0))
            if not 0 <= byte <= 255 or not 0 <= count <= max_poc_bytes:
                raise ValueError(f"operation {index}: invalid repeat")
            result.extend(bytes([byte]) * count)
        elif kind in {"u8", "u16", "u32", "u64"}:
            endian = str(operation.get("endian", "little"))
            if endian not in {"little", "big"}:
                raise ValueError(f"operation {index}: invalid endian")
            formats = {"u8": "B", "u16": "H", "u32": "I", "u64": "Q"}
            prefix = "<" if endian == "little" else ">"
            result.extend(struct.pack(prefix + formats[kind], int(operation.get("value", 0))))
        elif kind == "repeat_seq":
            sequence = _decode_sequence(operation.get("value"), str(operation.get("encoding", "hex")))
            count = int(operation.get("count", 0))
            if count < 0 or len(sequence) * count > max_poc_bytes:
                raise ValueError(f"operation {index}: invalid repeat_seq")
            result.extend(sequence * count)
        elif kind == "pad":
            byte = int(operation.get("byte", 0))
            target = int(operation.get("to", 0))
            if not 0 <= byte <= 255:
                raise ValueError(f"operation {index}: invalid pad byte")
            if target < len(result):
                raise ValueError(f"operation {index}: pad target {target} below current length {len(result)}")
            result.extend(bytes([byte]) * (target - len(result)))
        elif kind == "crc32":
            endian = str(operation.get("endian", "little"))
            if endian not in {"little", "big"}:
                raise ValueError(f"operation {index}: invalid endian")
            checksum = zlib.crc32(bytes(result)) & 0xFFFFFFFF
            result.extend(struct.pack("<I" if endian == "little" else ">I", checksum))
        elif kind == "patch":
            offset = int(operation.get("offset", 0))
            patch_bytes = _decode_sequence(operation.get("value"), str(operation.get("encoding", "hex")))
            if offset < 0 or offset + len(patch_bytes) > len(result):
                raise ValueError(f"operation {index}: patch out of range")
            result[offset:offset + len(patch_bytes)] = patch_bytes
        else:
            raise ValueError(f"operation {index}: unsupported type {kind!r}")
        if len(result) > max_poc_bytes:
            raise ValueError("PoC exceeds HARNESS_MAX_POC_BYTES")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "poc"
    path.write_bytes(result)
    return path


def make_python_sandbox(backend: str, image: str, timeout_seconds: int, max_poc_bytes: int) -> PythonSandbox:
    if backend == "docker":
        return DockerPythonSandbox(image, timeout_seconds, max_poc_bytes)
    if backend == "mock":
        return MockPythonSandbox()
    return DisabledPythonSandbox()

