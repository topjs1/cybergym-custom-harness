from __future__ import annotations

from dataclasses import replace

import pytest

from cybergym_harness.dynamic import DynamicTools, make_dynamic_backend
from cybergym_harness.dynamic.analysis import parse_asan
from cybergym_harness.dynamic.backend import DisabledDynamicBackend, DockerDynamicBackend
from cybergym_harness.dynamic.runner import ProcResult


class FakeRunner:
    def __init__(self, handler=None):
        self.calls: list[list[str]] = []
        self._handler = handler

    async def run(self, argv, timeout, env=None):
        self.calls.append(list(argv))
        if self._handler is not None:
            return self._handler(list(argv), timeout)
        return ProcResult(0, "", "")


def _inline(settings):
    return replace(
        settings,
        dynamic_analysis="inline",
        builder_image="builder:test",
        runner_image="runner:test",
    )


@pytest.mark.asyncio
async def test_docker_build_isolates_and_finds_targets(settings):
    configured = _inline(settings)
    ws = configured.work_root / "ws1"
    (ws / "repo-vul").mkdir(parents=True)
    (ws / "repo-vul" / ".git").mkdir()
    (ws / "repo-vul" / "build.sh").write_text("echo build")

    def handler(argv, timeout):
        out = ws / "build" / "out"
        target = out / "my_fuzzer"
        target.write_bytes(b"#!/bin/sh\n")
        target.chmod(0o755)
        return ProcResult(0, "build ok", "")

    runner = FakeRunner(handler)
    backend = DockerDynamicBackend(configured, configured.work_root, runner)
    result = await backend.build("ws1")
    assert result["ok"] is True
    assert result["targets"] == ["my_fuzzer"]
    assert not (ws / "repo-vul" / ".git").exists()
    argv = runner.calls[0]
    joined = " ".join(argv)
    assert "--network=none" in joined
    assert "no-new-privileges" in joined
    assert "builder:test" in argv


@pytest.mark.asyncio
async def test_docker_run_reports_crash(settings):
    configured = _inline(settings)
    ws = configured.work_root / "ws2"
    (ws / "build" / "out").mkdir(parents=True)
    (ws / "build" / "out" / "my_fuzzer").write_bytes(b"bin")
    (ws / "build" / "out" / "my_fuzzer").chmod(0o755)

    def handler(argv, timeout):
        return ProcResult(1, "", "ERROR: AddressSanitizer: heap-buffer-overflow\n#0 0x1 in parse /src/p.c:3")

    runner = FakeRunner(handler)
    backend = DockerDynamicBackend(configured, configured.work_root, runner)
    result = await backend.run("ws2", target="my_fuzzer", data=b"AAAA")
    assert result["ok"] is True
    assert result["exit_code"] == 1
    assert result["crashed"] is True
    joined = " ".join(runner.calls[0])
    assert "--network=none" in joined and "--read-only" in joined
    assert "runner:test" in runner.calls[0]


@pytest.mark.asyncio
async def test_docker_fuzz_collects_crashes(settings):
    configured = _inline(settings)
    ws = configured.work_root / "ws3"
    (ws / "build" / "out").mkdir(parents=True)
    (ws / "build" / "out" / "my_fuzzer").write_bytes(b"bin")
    (ws / "build" / "out" / "my_fuzzer").chmod(0o755)

    def handler(argv, timeout):
        crashes = ws / "build" / "crashes"
        (crashes / "crash-abc").write_bytes(b"BOOM")
        return ProcResult(0, "done", "")

    runner = FakeRunner(handler)
    backend = DockerDynamicBackend(configured, configured.work_root, runner)
    result = await backend.fuzz("ws3", target="my_fuzzer", seed=b"seed", seconds=5)
    assert result["ok"] is True
    assert len(result["crashes"]) == 1
    assert result["crashes"][0]["name"] == "crash-abc"


@pytest.mark.asyncio
async def test_mount_flags_use_valid_docker_syntax(settings):
    configured = _inline(settings)
    ws = configured.work_root / "wsm"
    (ws / "repo-vul").mkdir(parents=True)
    (ws / "repo-vul" / "build.sh").write_text("x")

    def handler(argv, timeout):
        out = ws / "build" / "out"
        out.mkdir(parents=True, exist_ok=True)
        target = out / "t"
        target.write_bytes(b"x")
        target.chmod(0o755)
        return ProcResult(0, "", "")

    runner = FakeRunner(handler)
    backend = DockerDynamicBackend(configured, configured.work_root, runner)
    await backend.build("wsm")
    await backend.run("wsm", target="t", data=b"a")
    joined = "\n".join(" ".join(call) for call in runner.calls)
    # Docker --mount accepts 'readonly'; the '-v' shorthand 'rw'/'ro' is invalid here.
    assert ",rw" not in joined
    assert ",readonly" in joined


@pytest.mark.asyncio
async def test_dynamic_tools_persists_crashes_and_submits(settings):
    configured = _inline(settings)
    ws = configured.work_root / "ws4"
    (ws / "build" / "out").mkdir(parents=True)
    (ws / "build" / "out" / "my_fuzzer").write_bytes(b"bin")
    (ws / "build" / "out" / "my_fuzzer").chmod(0o755)
    (ws / "generated").mkdir(parents=True)

    def handler(argv, timeout):
        crashes = ws / "build" / "crashes"
        (crashes / "crash-abc").write_bytes(b"BOOM")
        return ProcResult(0, "done", "")

    backend = DockerDynamicBackend(configured, configured.work_root, FakeRunner(handler))
    tools = DynamicTools(backend, configured, ws, "ws4")
    text = await tools.fuzz(seed_path="", seconds=5, target="my_fuzzer")
    assert "crash-" in text
    generated = sorted(path.name for path in (ws / "generated").iterdir())
    assert any(name.startswith("crash-") for name in generated)


@pytest.mark.asyncio
async def test_disabled_backend_reports_off(settings):
    backend = DisabledDynamicBackend()
    result = await backend.build("ws")
    assert result["ok"] is False
    assert "disabled" in result["error"]


def test_make_dynamic_backend_respects_mode(settings, tmp_path):
    off = make_dynamic_backend(settings, tmp_path)
    assert isinstance(off, DisabledDynamicBackend)
    sidecar = make_dynamic_backend(replace(settings, dynamic_analysis="sidecar"), tmp_path)
    assert sidecar.__class__.__name__ == "SidecarDynamicBackend"
    inline = make_dynamic_backend(replace(settings, dynamic_analysis="inline"), tmp_path, runner=FakeRunner())
    assert isinstance(inline, DockerDynamicBackend)


@pytest.mark.asyncio
async def test_discover_crash_pipeline_collects_fuzzes_minimizes(settings):
    configured = _inline(settings)
    ws = configured.work_root / "wsd"
    (ws / "repo-vul" / "testdata").mkdir(parents=True)
    (ws / "repo-vul" / "testdata" / "seed1").write_bytes(b"seedbytes")
    out = ws / "build" / "out"
    out.mkdir(parents=True)
    target = out / "fuzzer"
    target.write_bytes(b"x")
    target.chmod(0o755)

    def handler(argv, timeout):
        crashes = ws / "build" / "crashes"
        crashes.mkdir(parents=True, exist_ok=True)
        if "-minimize_crash=1" in argv:
            (crashes / "minimized").write_bytes(b"BOOM")
        else:
            (crashes / "crash-abc").write_bytes(b"BOOMLONGER")
        return ProcResult(0, "", "")

    runner = FakeRunner(handler)
    backend = DockerDynamicBackend(configured, configured.work_root, runner)
    result = await backend.discover_crash("wsd", target="fuzzer", seconds=5)
    assert result["ok"] is True
    assert result["seeds"] >= 1
    assert len(result["crashes"]) == 1
    assert result["minimized"]["name"] == "minimized"
    calls = [" ".join(call) for call in runner.calls]
    assert any("-minimize_crash=1" in call for call in calls)


@pytest.mark.asyncio
async def test_discover_crash_tool_persists_minimized(settings):
    configured = _inline(settings)
    ws = configured.work_root / "wsdt"
    (ws / "repo-vul" / "seeds").mkdir(parents=True)
    (ws / "repo-vul" / "seeds" / "a").write_bytes(b"seed")
    out = ws / "build" / "out"
    out.mkdir(parents=True)
    target = out / "fuzzer"
    target.write_bytes(b"x")
    target.chmod(0o755)
    (ws / "generated").mkdir(parents=True)

    def handler(argv, timeout):
        crashes = ws / "build" / "crashes"
        crashes.mkdir(parents=True, exist_ok=True)
        if "-minimize_crash=1" in argv:
            (crashes / "minimized").write_bytes(b"BOOM")
        else:
            (crashes / "crash-abc").write_bytes(b"BOOMLONGER")
        return ProcResult(0, "", "")

    backend = DockerDynamicBackend(configured, configured.work_root, FakeRunner(handler))
    tools = DynamicTools(backend, configured, ws, "wsdt")
    text = await tools.discover_crash(target="fuzzer", seconds=5)
    assert "minimized-" in text
    names = sorted(p.name for p in (ws / "generated").iterdir())
    assert any(name.startswith("minimized-") for name in names)
    assert any(name.startswith("crash-") for name in names)


def test_parse_asan_extracts_function():
    text = (
        "ERROR: AddressSanitizer: heap-buffer-overflow on address 0x0\n"
        "#0 0x55 in parse_input /src/parser.c:42\n"
        "#1 0x66 in main /src/main.c:7\n"
    )
    parsed = parse_asan(text)
    assert parsed["crash_function"] == "parse_input /src/parser.c:42"
    assert parsed["locations"][0] == "/src/parser.c:42"
