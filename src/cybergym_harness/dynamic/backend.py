from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import httpx

from .analysis import parse_asan
from .policy import (
    TARGET_CONTAINER_FLAGS,
    builder_container_flags,
    ensure_within,
    resolve_member,
    sanitize_identifier,
    strip_leaks,
)
from .runner import CommandRunner, SubprocessRunner

_OUTPUT_LIMIT = 8000


def _tail(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return "...[truncated]\n" + text[-limit:]


class DynamicBackend:
    async def build(self, workspace_id: str, *, repo_subdir: str = "repo-vul", target_hint: str = "") -> dict[str, Any]:
        raise NotImplementedError

    async def run(self, workspace_id: str, *, target: str, data: bytes) -> dict[str, Any]:
        raise NotImplementedError

    async def fuzz(self, workspace_id: str, *, target: str, seed: bytes, seconds: int) -> dict[str, Any]:
        raise NotImplementedError

    async def triage(self, workspace_id: str, *, target: str, data: bytes) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class DisabledDynamicBackend(DynamicBackend):
    _MESSAGE = "dynamic analysis is disabled (set HARNESS_DYNAMIC_ANALYSIS=sidecar to enable it)"

    async def build(self, workspace_id: str, *, repo_subdir: str = "repo-vul", target_hint: str = "") -> dict[str, Any]:
        return {"ok": False, "error": self._MESSAGE}

    async def run(self, workspace_id: str, *, target: str, data: bytes) -> dict[str, Any]:
        return {"ok": False, "error": self._MESSAGE}

    async def fuzz(self, workspace_id: str, *, target: str, seed: bytes, seconds: int) -> dict[str, Any]:
        return {"ok": False, "error": self._MESSAGE}

    async def triage(self, workspace_id: str, *, target: str, data: bytes) -> dict[str, Any]:
        return {"ok": False, "error": self._MESSAGE}


class DockerDynamicBackend(DynamicBackend):
    """Builds and fuzzes the vulnerable target in ephemeral, isolated containers."""

    def __init__(self, settings: Any, workspace_root: Path, runner: CommandRunner | None = None):
        self.settings = settings
        self.workspace_root = Path(workspace_root)
        self.runner = runner or SubprocessRunner()

    def _ws(self, workspace_id: str) -> Path:
        return ensure_within(self.workspace_root, self.workspace_root / sanitize_identifier(workspace_id))

    def _docker(self) -> str:
        return shutil.which("docker") or "docker"

    def _host_path(self, path: Path) -> str:
        """Translate a path under workspace_root to the daemon host's view of it.

        When the harness runs inside a container that talks to the host Docker
        daemon, bind-mount sources are resolved on the host, not in this
        container. HARNESS_DYNAMIC_HOST_ROOT provides that mapping.
        """
        resolved = path.resolve()
        host_root = getattr(self.settings, "dynamic_host_root", None)
        if not host_root:
            return str(resolved)
        try:
            relative = resolved.relative_to(self.workspace_root.resolve())
        except ValueError:
            return str(resolved)
        return str((Path(host_root) / relative).resolve())

    async def build(self, workspace_id: str, *, repo_subdir: str = "repo-vul", target_hint: str = "") -> dict[str, Any]:
        try:
            ws = self._ws(workspace_id)
            repo = ensure_within(ws, ws / repo_subdir)
            if not repo.is_dir():
                return {"ok": False, "error": f"repository directory not found: {repo_subdir}"}
            removed = strip_leaks(repo)
            out = ws / "build" / "out"
            out.mkdir(parents=True, exist_ok=True)
            os.chmod(out, 0o777)
            flags = builder_container_flags(self.settings.build_allow_network)
            env = {
                "SANITIZER": "address",
                "ARCHITECTURE": "x86_64",
                "FUZZING_ENGINE": "libfuzzer",
                "CFLAGS": "-fsanitize=address -g -O1",
                "CXXFLAGS": "-fsanitize=address -g -O1",
            }
            argv = [
                self._docker(), "run", *flags,
                "--mount", f"type=bind,src={self._host_path(repo)},dst=/src",
                "--mount", f"type=bind,src={self._host_path(out)},dst=/out",
                "-w", "/src",
            ]
            for key, value in env.items():
                argv += ["-e", f"{key}={value}"]
            argv += [self.settings.builder_image, "/bin/bash", "-lc", self.settings.build_command]
            result = await self.runner.run(argv, timeout=self.settings.build_timeout_seconds, env=env)
            targets = sorted(
                path.name for path in out.iterdir()
                if path.is_file() and os.access(path, os.X_OK)
            )
            return {
                "ok": result.returncode == 0,
                "exit_code": result.returncode,
                "targets": targets,
                "removed_leaks": removed,
                "log": _tail(result.combined),
            }
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def run(self, workspace_id: str, *, target: str, data: bytes) -> dict[str, Any]:
        try:
            ws = self._ws(workspace_id)
            out = ensure_within(ws, ws / "build" / "out")
            target_path = resolve_member(out, target)
            if not target_path.is_file():
                return {"ok": False, "error": f"target not found: {target}"}
            inputs = ws / "build" / "inputs"
            inputs.mkdir(parents=True, exist_ok=True)
            (inputs / "input").write_bytes(data)
            argv = [
                self._docker(), "run", *TARGET_CONTAINER_FLAGS,
                "--mount", f"type=bind,src={self._host_path(out)},dst=/out,readonly",
                "--mount", f"type=bind,src={self._host_path(inputs)},dst=/inputs,readonly",
                self.settings.runner_image,
                f"/out/{target_path.name}", "/inputs/input",
            ]
            result = await self.runner.run(argv, timeout=self.settings.dynamic_timeout_seconds)
            return {
                "ok": True,
                "exit_code": result.returncode,
                "crashed": result.returncode != 0,
                "output": _tail(result.combined),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def fuzz(self, workspace_id: str, *, target: str, seed: bytes, seconds: int) -> dict[str, Any]:
        try:
            if self.settings.fuzz_engine != "libfuzzer":
                return {"ok": False, "error": f"fuzz engine {self.settings.fuzz_engine!r} is not supported yet; use libfuzzer"}
            ws = self._ws(workspace_id)
            out = ensure_within(ws, ws / "build" / "out")
            target_path = resolve_member(out, target)
            if not target_path.is_file():
                return {"ok": False, "error": f"target not found: {target}"}
            corpus = ws / "build" / "corpus"
            crashes = ws / "build" / "crashes"
            corpus.mkdir(parents=True, exist_ok=True)
            crashes.mkdir(parents=True, exist_ok=True)
            os.chmod(crashes, 0o777)
            if seed:
                (corpus / "seed").write_bytes(seed)
            argv = [
                self._docker(), "run", *TARGET_CONTAINER_FLAGS,
                "--mount", f"type=bind,src={self._host_path(out)},dst=/out,readonly",
                "--mount", f"type=bind,src={self._host_path(corpus)},dst=/corpus,readonly",
                "--mount", f"type=bind,src={self._host_path(crashes)},dst=/crashes",
                self.settings.runner_image,
                f"/out/{target_path.name}",
                f"-max_total_time={seconds}",
                "-artifact_prefix=/crashes/",
                "/corpus",
            ]
            result = await self.runner.run(argv, timeout=seconds + self.settings.dynamic_timeout_seconds)
            found = []
            for path in sorted(crashes.iterdir()):
                if path.is_file():
                    blob = path.read_bytes()
                    found.append({
                        "name": path.name,
                        "size": len(blob),
                        "sha256": hashlib.sha256(blob).hexdigest(),
                    })
            return {
                "ok": True,
                "crashes": found[: self.settings.max_crashes],
                "output": _tail(result.combined),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def triage(self, workspace_id: str, *, target: str, data: bytes) -> dict[str, Any]:
        run_result = await self.run(workspace_id, target=target, data=data)
        if not run_result.get("ok"):
            return run_result
        text = str(run_result.get("output", ""))
        return {
            "ok": True,
            "exit_code": run_result.get("exit_code"),
            "crash": parse_asan(text),
            "output": text,
        }

    async def close(self) -> None:
        return None


class SidecarDynamicBackend(DynamicBackend):
    """Forwards dynamic operations to the isolated dynamic-sandbox sidecar."""

    def __init__(self, base_url: str, timeout_seconds: int = 120, token: str = ""):
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds),
            headers=headers,
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"sidecar transport error: {type(exc).__name__}: {exc}"}
        if response.status_code >= 400:
            return {"ok": False, "error": f"sidecar HTTP {response.status_code}: {response.text[:500]}"}
        try:
            data = response.json()
        except ValueError:
            return {"ok": False, "error": "sidecar returned invalid JSON"}
        return data if isinstance(data, dict) else {"ok": False, "error": "sidecar returned unexpected payload"}

    async def build(self, workspace_id: str, *, repo_subdir: str = "repo-vul", target_hint: str = "") -> dict[str, Any]:
        return await self._post("/build", {
            "workspace_id": workspace_id,
            "repo_subdir": repo_subdir,
            "target_hint": target_hint,
        })

    async def run(self, workspace_id: str, *, target: str, data: bytes) -> dict[str, Any]:
        return await self._post("/run", {
            "workspace_id": workspace_id,
            "target": target,
            "data_b64": base64.b64encode(data).decode("ascii"),
        })

    async def fuzz(self, workspace_id: str, *, target: str, seed: bytes, seconds: int) -> dict[str, Any]:
        return await self._post("/fuzz", {
            "workspace_id": workspace_id,
            "target": target,
            "seed_b64": base64.b64encode(seed).decode("ascii") if seed else "",
            "seconds": seconds,
        })

    async def triage(self, workspace_id: str, *, target: str, data: bytes) -> dict[str, Any]:
        return await self._post("/triage", {
            "workspace_id": workspace_id,
            "target": target,
            "data_b64": base64.b64encode(data).decode("ascii"),
        })

    async def close(self) -> None:
        await self._client.aclose()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
