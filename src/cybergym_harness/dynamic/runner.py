from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence


@dataclass(slots=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def combined(self) -> str:
        text = self.stdout
        if self.stderr:
            text = f"{text}\n{self.stderr}" if text else self.stderr
        return text


class CommandRunner(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> ProcResult: ...


class SubprocessRunner:
    """Runs a local command with a bounded timeout and a scrubbed environment."""

    def __init__(self, base_env: Mapping[str, str] | None = None):
        self._base_env = dict(base_env or {"PATH": os.environ.get("PATH", "")})

    async def run(
        self,
        argv: Sequence[str],
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> ProcResult:
        merged = dict(self._base_env)
        if env:
            merged.update(env)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return ProcResult(-1, "", f"timed out after {timeout}s", timed_out=True)
        return ProcResult(
            proc.returncode if proc.returncode is not None else -1,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
