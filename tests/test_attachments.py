from __future__ import annotations

import base64
import io
import tarfile

import pytest
from a2a.types import FilePart, FileWithBytes, Message, Part, Role

from cybergym_harness.attachments import prepare_challenge
from tests.helpers import challenge_message


def test_prepare_challenge_extracts_and_identifies_level(tmp_path):
    challenge = prepare_challenge(
        challenge_message(), "context-1", tmp_path, 4_000_000, 16_000_000
    )
    assert challenge.task_id == "mock:0001"
    assert challenge.task_id_source == "message_data"
    assert challenge.difficulty == "level2"
    assert (challenge.input_root / "repo-vul/sample/fuzz.c").is_file()
    assert (challenge.input_root / "description.txt").read_text().startswith("Mock")


def _tar_with_links() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = b"hello"
        info = tarfile.TarInfo("dir/a.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        safe = tarfile.TarInfo("dir/link")
        safe.type = tarfile.SYMTYPE
        safe.linkname = "a.txt"
        archive.addfile(safe)
        evil = tarfile.TarInfo("dir/evil")
        evil.type = tarfile.SYMTYPE
        evil.linkname = "../../../../etc/passwd"
        archive.addfile(evil)
    return buffer.getvalue()


def test_safe_symlinks_extract_and_unsafe_are_skipped(tmp_path):
    part = Part(root=FilePart(file=FileWithBytes(
        bytes=base64.b64encode(_tar_with_links()).decode("ascii"),
        name="repo-vul.tar.gz",
        mime_type="application/gzip",
    )))
    message = Message(kind="message", role=Role.user, parts=[part], message_id="links")
    challenge = prepare_challenge(message, "context-links", tmp_path, 4_000_000, 16_000_000)
    link = challenge.input_root / "repo-vul/dir/link"
    assert link.is_symlink()
    assert (challenge.input_root / "repo-vul/dir/a.txt").is_file()
    assert not (challenge.input_root / "repo-vul/dir/evil").exists()


def test_archive_path_traversal_is_rejected(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = b"bad"
        info = tarfile.TarInfo("../../escape")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    part = Part(root=FilePart(file=FileWithBytes(
        bytes=base64.b64encode(buffer.getvalue()).decode("ascii"),
        name="repo-vul.tar.gz",
        mime_type="application/gzip",
    )))
    message = Message(kind="message", role=Role.user, parts=[part], message_id="bad")
    with pytest.raises(ValueError, match="unsafe archive path"):
        prepare_challenge(message, "context-2", tmp_path, 4_000_000, 16_000_000)

