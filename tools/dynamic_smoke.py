"""Real Docker smoke test for the dynamic backend (no CyberGym data, no LLM).

Builds a tiny synthetic target in one container and runs it in another, exercising
the real DockerDynamicBackend: bind mounts, isolation flags, exit-code propagation.

    uv run python tools/dynamic_smoke.py                 # uses ubuntu:22.04
    HARNESS_BUILDER_IMAGE=gcc:13 uv run python tools/dynamic_smoke.py
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

TARGET_SH = """#!/bin/sh
# Synthetic vulnerable target: crash (exit 139) when the input contains the magic word.
input="$1"
if [ -z "$input" ] || [ ! -f "$input" ]; then
  echo "no input"; exit 0
fi
if grep -q "MAGIC" "$input" 2>/dev/null; then
  echo "synthetic crash: MAGIC found"; exit 139
fi
echo "no crash"; exit 0
"""

BUILD_SH = """#!/bin/sh
set -e
cp "$(dirname "$0")/target.sh" /out/target
chmod 0755 /out/target
echo "built /out/target"
"""


def _prepare_repo(root: Path) -> Path:
    repo = root / "repo-vul"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "target.sh").write_text(TARGET_SH)
    (repo / "build.sh").write_text(BUILD_SH)
    (repo / "input_parser.c").write_text("/* fake source for triage realism */\n")
    (repo / ".git").mkdir(exist_ok=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "patch.diff").write_text("--- a\n+++ b\n")
    return repo


async def main() -> int:
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = PROJECT / "data" / f"dynamic-smoke-{ts}"
    work_root = out_dir / "tasks"
    workspace_id = "smoke1"
    root = work_root / workspace_id
    root.mkdir(parents=True, exist_ok=True)
    repo = _prepare_repo(root)

    settings = replace(
        Settings.from_env(),
        dynamic_analysis="inline",
        builder_image="ubuntu:22.04",
        runner_image="ubuntu:22.04",
        build_command="bash build.sh",
        build_allow_network=False,
        work_root=work_root,
    )
    if not shutil.which("docker"):
        print("FAIL: docker CLI not available")
        return 2

    backend = DockerDynamicBackend(settings, work_root)
    print(f"images: builder/runner = {settings.builder_image}")
    print(f"workspace: {root}")

    build = await backend.build(workspace_id, repo_subdir="repo-vul")
    print("\n[build]", {k: build.get(k) for k in ("ok", "exit_code", "targets", "removed_leaks")})
    print("build log:", str(build.get("log", "")).strip()[-400:])
    if not build.get("ok") or "target" not in build.get("targets", []):
        print("FAIL: build did not produce the target")
        return 1

    benign = await backend.run(workspace_id, target="target", data=b"hello world")
    crash = await backend.run(workspace_id, target="target", data=b"xxMAGICxx")
    print("\n[run benign]", {k: benign.get(k) for k in ("ok", "exit_code", "crashed")})
    print("[run magic ]", {k: crash.get(k) for k in ("ok", "exit_code", "crashed")})
    print("magic output:", str(crash.get("output", "")).strip()[-200:])

    triage = await backend.triage(workspace_id, target="target", data=b"xxMAGICxx")
    print("\n[triage]", {"ok": triage.get("ok"), "exit_code": triage.get("exit_code")})

    ok = (
        build.get("ok")
        and benign.get("ok") and benign.get("exit_code") == 0 and not benign.get("crashed")
        and crash.get("ok") and crash.get("exit_code") != 0 and crash.get("crashed")
    )
    # leak stripping should have removed .git and patch.diff from the repo copy
    leaks_gone = not (repo / ".git").exists() and not (repo / "patch.diff").exists()
    print(f"\nleak strip: {'OK' if leaks_gone else 'FAILED'}")
    print("RESULT:", "PASS" if (ok and leaks_gone) else "FAIL")
    return 0 if (ok and leaks_gone) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
