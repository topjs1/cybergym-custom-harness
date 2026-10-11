from __future__ import annotations

import base64
import hmac
import os

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cybergym_harness.config import Settings
from cybergym_harness.dynamic.backend import DockerDynamicBackend

_SETTINGS = Settings.from_env()
_BACKEND = DockerDynamicBackend(_SETTINGS, _SETTINGS.work_root)
_TOKEN = os.getenv("HARNESS_SIDECAR_TOKEN", "").strip()


def _authorized(request: Request) -> bool:
    if not _TOKEN:
        return True
    header = request.headers.get("authorization", "")
    expected = f"Bearer {_TOKEN}"
    return hmac.compare_digest(header, expected)


def _decode(value: str) -> bytes:
    if not value:
        return b""
    return base64.b64decode(value)


async def _json(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _guard(request: Request) -> JSONResponse | None:
    if not _authorized(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return None


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "mode": _SETTINGS.dynamic_analysis})


async def build(request: Request) -> JSONResponse:
    if (guard := _guard(request)) is not None:
        return guard
    payload = await _json(request)
    result = await _BACKEND.build(
        str(payload.get("workspace_id", "")),
        repo_subdir=str(payload.get("repo_subdir", "repo-vul")),
        target_hint=str(payload.get("target_hint", "")),
    )
    return JSONResponse(result)


async def run(request: Request) -> JSONResponse:
    if (guard := _guard(request)) is not None:
        return guard
    payload = await _json(request)
    result = await _BACKEND.run(
        str(payload.get("workspace_id", "")),
        target=str(payload.get("target", "")),
        data=_decode(str(payload.get("data_b64", ""))),
    )
    return JSONResponse(result)


async def fuzz(request: Request) -> JSONResponse:
    if (guard := _guard(request)) is not None:
        return guard
    payload = await _json(request)
    result = await _BACKEND.fuzz(
        str(payload.get("workspace_id", "")),
        target=str(payload.get("target", "")),
        seed=_decode(str(payload.get("seed_b64", ""))),
        seconds=int(payload.get("seconds") or _SETTINGS.fuzz_seconds),
    )
    return JSONResponse(result)


async def triage(request: Request) -> JSONResponse:
    if (guard := _guard(request)) is not None:
        return guard
    payload = await _json(request)
    result = await _BACKEND.triage(
        str(payload.get("workspace_id", "")),
        target=str(payload.get("target", "")),
        data=_decode(str(payload.get("data_b64", ""))),
    )
    return JSONResponse(result)


app = Starlette(
    routes=[
        Route("/healthz", health, methods=["GET"]),
        Route("/build", build, methods=["POST"]),
        Route("/run", run, methods=["POST"]),
        Route("/fuzz", fuzz, methods=["POST"]),
        Route("/triage", triage, methods=["POST"]),
    ]
)


def main() -> None:
    host = os.getenv("SIDECAR_HOST", "0.0.0.0")
    port = int(os.getenv("SIDECAR_PORT", "9110"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
