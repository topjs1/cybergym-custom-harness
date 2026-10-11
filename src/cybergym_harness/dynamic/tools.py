from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

from .backend import DynamicBackend, dumps
from .policy import ensure_within, safe_relative


class DynamicTools:
    """Adapts LLM tool arguments to the dynamic backend and renders bounded text."""

    def __init__(self, backend: DynamicBackend, settings: Any, workspace: Path, workspace_id: str):
        self.backend = backend
        self.settings = settings
        self.workspace = workspace
        self.workspace_id = workspace_id
        self.generated = workspace / "generated"

    def _read_input(self, input_path: str, input_base64: str) -> bytes:
        if input_base64:
            try:
                return base64.b64decode(input_base64, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise ValueError("input_base64 is not valid base64") from exc
        if not input_path:
            raise ValueError("provide either input_path or input_base64")
        name = input_path
        for prefix in ("generated/", "generated"):
            if name.startswith(prefix):
                name = name[len(prefix):].lstrip("/") or name
                break
        target = ensure_within(self.generated, self.generated / safe_relative(name))
        if not target.is_file():
            raise ValueError(f"generated/{name} does not exist")
        return target.read_bytes()

    async def build(self, repo_subdir: str = "repo-vul", target_hint: str = "") -> str:
        result = await self.backend.build(self.workspace_id, repo_subdir=repo_subdir, target_hint=target_hint)
        if not result.get("ok"):
            return f"build_target FAILED: {result.get('error') or result.get('log', 'unknown error')}"
        targets = result.get("targets", [])
        if not targets:
            return (
                "build_target completed but no executable fuzz target was found in /out. "
                f"log:\n{result.get('log', '')}"
            )
        return f"build_target OK. targets={targets}\nlog:\n{result.get('log', '')}"

    async def run(self, input_path: str = "", input_base64: str = "", target: str = "") -> str:
        if not target:
            return "run_target requires a target name (see build_target output)"
        try:
            data = self._read_input(input_path, input_base64)
        except ValueError as exc:
            return f"run_target error: {exc}"
        result = await self.backend.run(self.workspace_id, target=target, data=data)
        if not result.get("ok"):
            return f"run_target FAILED: {result.get('error')}"
        code = result.get("exit_code")
        verdict = "CRASH" if result.get("crashed") else "no crash"
        return f"run_target exit_code={code} ({verdict})\n{result.get('output', '')}"

    async def fuzz(self, seed_path: str = "", seconds: int | None = None, target: str = "") -> str:
        if not target:
            return "fuzz_target requires a target name (see build_target output)"
        duration = min(int(seconds or self.settings.fuzz_seconds), self.settings.fuzz_seconds)
        try:
            seed = self._read_input(seed_path, "") if seed_path else b""
        except ValueError as exc:
            return f"fuzz_target error: {exc}"
        result = await self.backend.fuzz(self.workspace_id, target=target, seed=seed, seconds=duration)
        if not result.get("ok"):
            return f"fuzz_target FAILED: {result.get('error')}"
        saved = self._persist_crashes(result.get("crashes", []))
        if not saved:
            return f"fuzz_target found no crashes in {duration}s\n{result.get('output', '')}"
        return (
            f"fuzz_target found {len(saved)} crash(es). Submit one with submit_poc path=<name>.\n"
            f"crashes={saved}\n{result.get('output', '')}"
        )

    async def triage(self, input_path: str = "", input_base64: str = "", target: str = "") -> str:
        if not target:
            return "triage_crash requires a target name (see build_target output)"
        try:
            data = self._read_input(input_path, input_base64)
        except ValueError as exc:
            return f"triage_crash error: {exc}"
        result = await self.backend.triage(self.workspace_id, target=target, data=data)
        if not result.get("ok"):
            return f"triage_crash FAILED: {result.get('error')}"
        return f"triage_crash exit_code={result.get('exit_code')} crash={dumps(result.get('crash'))}"

    def _persist_crashes(self, crashes: list[dict[str, Any]]) -> list[str]:
        crashes_dir = self.workspace / "build" / "crashes"
        names: list[str] = []
        for item in crashes:
            name = str(item.get("name", ""))
            if not name or Path(name).name != name:
                continue
            source = ensure_within(crashes_dir, crashes_dir / name)
            if not source.is_file():
                continue
            blob = source.read_bytes()
            digest = hashlib.sha256(blob).hexdigest()[:16]
            destination = self.generated / f"crash-{digest}"
            destination.write_bytes(blob)
            names.append(f"{destination.name} ({len(blob)} bytes, sha256={item.get('sha256')})")
        return names
