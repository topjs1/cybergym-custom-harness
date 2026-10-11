"""Real Docker smoke test for the find_crash pipeline (find seeds -> fuzz -> minimize).

Builds a tiny libFuzzer target with clang (installed during the build step) that
crashes on the magic input "BOOM", seeds the corpus via a testdata/ directory, then
runs DockerDynamicBackend.discover_crash end to end.

    uv run python tools/find_crash_smoke.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from cybergym_harness.config import Settings  # noqa: E402
from cybergym_harness.dynamic.backend import DockerDynamicBackend  # noqa: E402

FUZZ_C = r"""
#include <stdint.h>
#include <stddef.h>
int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  if (size >= 4 && data[0] == 'B' && data[1] == 'O' && data[2] == 'O' && data[3] == 'M') {
    volatile int *p = (volatile int *)0;
    *p = 1;
  }
  return 0;
}
"""

BUILD_SH = """#!/bin/sh
set -e
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq clang >/dev/null 2>&1
clang -fsanitize=fuzzer,address -O1 -g ./fuzz.c -o /out/fuzzer
echo "built /out/fuzzer"
"""


def _prepare_repo(root: Path) -> Path:
    repo = root / "repo-vul"
    (repo / "testdata").mkdir(parents=True, exist_ok=True)
    (repo / "fuzz.c").write_text(FUZZ_C)
    (repo / "build.sh").write_text(BUILD_SH)
    # long seed corpus so the crash is found fast AND minimize visibly shrinks it
    (repo / "testdata" / "seed").write_bytes(b"BOOM" + b"A" * 200)
    return repo


async def main() -> int:
    if not shutil.which("docker"):
        print("FAIL: docker CLI not available")
        return 2
    ts = time.strftime("%Y%m%d-%H%M%S")
    work_root = PROJECT / "data" / f"find-crash-smoke-{ts}" / "tasks"
    workspace_id = "smoke1"
    root = work_root / workspace_id
    root.mkdir(parents=True, exist_ok=True)
    _prepare_repo(root)

    settings = replace(
        Settings.from_env(),
        dynamic_analysis="inline",
        builder_image="ubuntu:22.04",
        runner_image="ubuntu:22.04",
        build_command="bash build.sh",
        build_allow_network=True,
        work_root=work_root,
        fuzz_seconds=20,
        dynamic_timeout_seconds=60,
    )
    backend = DockerDynamicBackend(settings, work_root)
    print(f"builder/runner = {settings.builder_image}; workspace = {root}")

    build = await backend.build(workspace_id, repo_subdir="repo-vul")
    print("\n[build]", {k: build.get(k) for k in ("ok", "exit_code", "targets")})
    if not build.get("ok") or not build.get("targets"):
        print("build log:", str(build.get("log", ""))[-800:])
        print("RESULT: FAIL (build)")
        return 1
    target = build["targets"][0]

    result = await backend.discover_crash(
        workspace_id, target=target, seconds=20, repo_subdir="repo-vul", minimize_seconds=30
    )
    print("\n[find_crash]", {
        "ok": result.get("ok"),
        "seeds": result.get("seeds"),
        "crashes": [c.get("name") for c in result.get("crashes", [])],
        "minimized": result.get("minimized"),
    })
    if result.get("minimized"):
        print("minimized size:", result["minimized"].get("size"))

    ok = bool(result.get("ok")) and bool(result.get("crashes")) and bool(result.get("minimized"))
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
