from __future__ import annotations

from pathlib import Path

import pytest

from cybergym_harness.dynamic.policy import (
    TARGET_CONTAINER_FLAGS,
    builder_container_flags,
    ensure_within,
    safe_relative,
    strip_leaks,
)


def test_target_flags_are_fully_isolated():
    joined = " ".join(TARGET_CONTAINER_FLAGS)
    assert "--network=none" in joined
    assert "--read-only" in joined
    assert "--cap-drop=ALL" in joined
    assert "no-new-privileges" in joined


def test_builder_network_is_opt_in():
    assert "--network=none" in builder_container_flags(False)
    assert "--network=bridge" in builder_container_flags(True)


def test_ensure_within_rejects_escape(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    assert ensure_within(root, root / "child") == (root / "child").resolve()
    with pytest.raises(ValueError):
        ensure_within(root, tmp_path / "other")


def test_safe_relative_blocks_traversal():
    assert safe_relative("repo-vul/src/a.c") == "repo-vul/src/a.c"
    for bad in ("", "/etc/passwd", "../x", "a/../../b", "a\\b"):
        with pytest.raises(ValueError):
            safe_relative(bad)


def test_strip_leaks_removes_answer_artifacts(tmp_path: Path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref")
    (root / "patch.diff").write_text("--- a\n+++ b\n")
    (root / "tmp").mkdir()
    (root / "tmp" / "poc").write_bytes(b"reference")
    (root / "keep.c").write_text("int main(){}")
    removed = strip_leaks(root)
    assert not (root / ".git").exists()
    assert not (root / "patch.diff").exists()
    assert not (root / "tmp" / "poc").exists()
    assert (root / "keep.c").exists()
    assert removed
