from __future__ import annotations

import re
import shutil
from pathlib import Path

TARGET_CONTAINER_FLAGS = [
    "--rm",
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--pids-limit=256",
    "--memory=1g",
    "--memory-swap=1g",
    "--cpus=1.0",
    "--user=65534:65534",
    "--tmpfs",
    "/tmp:rw,nosuid,nodev,noexec,size=64m",
]

# Builders run as root by design: OSS-Fuzz build.sh writes into the mounted
# source and output trees and expects root. The builder is still sandboxed
# (cap-drop, no-new-privileges, resource caps, optional no-network) and never
# receives secrets or host paths beyond the per-task workspace.
BUILDER_CONTAINER_FLAGS_BASE = [
    "--rm",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--pids-limit=512",
    "--memory=4g",
    "--memory-swap=4g",
    "--cpus=2.0",
    "--tmpfs",
    "/tmp:rw,nosuid,nodev,noexec,size=256m",
]

_SAFE_IDENTIFIER = re.compile(r"[^A-Za-z0-9._-]")
_SAFE_RELATIVE = re.compile(r"^[A-Za-z0-9._/-]+$")


def builder_container_flags(allow_network: bool) -> list[str]:
    flags = list(BUILDER_CONTAINER_FLAGS_BASE)
    flags.append("--network=bridge" if allow_network else "--network=none")
    return flags


def sanitize_identifier(value: str, *, limit: int = 128) -> str:
    cleaned = _SAFE_IDENTIFIER.sub("_", value.strip())
    return cleaned[:limit]


def ensure_within(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("path escapes the dynamic workspace")
    return resolved


def safe_relative(value: str) -> str:
    if not value or value.startswith("/") or "\x00" in value:
        raise ValueError("path must be a non-empty relative path")
    if ".." in Path(value).parts:
        raise ValueError("path must not contain '..'")
    if not _SAFE_RELATIVE.fullmatch(value):
        raise ValueError(f"path contains unsupported characters: {value!r}")
    return value


def resolve_member(root: Path, relative: str) -> Path:
    return ensure_within(root, root / safe_relative(relative))


def strip_leaks(root: Path) -> list[str]:
    """Remove answer-leaking artifacts before handing a tree to dynamic tools."""
    removed: list[str] = []
    if not root.exists():
        return removed
    for git_dir in list(root.rglob(".git")):
        if git_dir.is_dir():
            shutil.rmtree(git_dir, ignore_errors=True)
            removed.append(str(git_dir.relative_to(root)))
    for path in list(root.rglob("patch.diff")):
        if path.is_file():
            path.unlink(missing_ok=True)
            removed.append(str(path.relative_to(root)))
    for leak_dir in ("tmp", "out"):
        candidate = root / leak_dir / "poc"
        if candidate.is_file():
            candidate.unlink(missing_ok=True)
            removed.append(str(Path(leak_dir) / "poc"))
    return removed
