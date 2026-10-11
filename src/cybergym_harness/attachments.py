from __future__ import annotations

import base64
import hashlib
import io
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from a2a.types import DataPart, FilePart, FileWithBytes, Message, TextPart


@dataclass(slots=True)
class Challenge:
    task_id: str
    task_id_source: str
    prompt: str
    difficulty: str
    workspace: Path
    input_root: Path
    attachment_names: list[str]


def _safe_filename(name: str | None) -> str:
    if not name:
        raise ValueError("attachment has no name")
    path = PurePosixPath(name)
    if path.is_absolute() or len(path.parts) != 1 or path.name in {"", ".", ".."}:
        raise ValueError(f"unsafe attachment name: {name!r}")
    return path.name


def _safe_extract_tar(data: bytes, destination: Path, max_extracted_bytes: int) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        members = archive.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"unsafe archive path: {member.name!r}")
            total += max(member.size, 0)
            if total > max_extracted_bytes:
                raise ValueError("archive exceeds HARNESS_MAX_EXTRACTED_BYTES")

        for member in members:
            target = (destination / member.name).resolve()
            if target != base and base not in target.parents:
                raise ValueError(f"archive entry escapes destination: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            # Links: allow contained symlinks/hardlinks; skip anything unsafe (absolute or
            # escaping) and all devices/FIFOs instead of failing the whole task.
            if member.issym() or member.islnk():
                link = PurePosixPath(member.linkname)
                if link.is_absolute():
                    continue
                link_target = (target.parent / member.linkname).resolve()
                if link_target != base and base not in link_target.parents:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if member.islnk() and (destination / member.linkname).is_file():
                    shutil.copyfile(destination / member.linkname, target)
                    continue
                try:
                    target.unlink(missing_ok=True)
                    target.symlink_to(member.linkname)
                except OSError:
                    continue
                continue
            if member.isdev() or member.isfifo() or not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _difficulty(names: set[str]) -> str:
    if {"repo-fix.tar.gz", "patch.diff"}.issubset(names):
        return "level3"
    if "error.txt" in names:
        return "level2"
    if "description.txt" in names:
        return "level1"
    return "level0"


def prepare_challenge(
    message: Message,
    context_id: str,
    work_root: Path,
    max_attachment_bytes: int,
    max_extracted_bytes: int,
) -> Challenge:
    workspace_key = hashlib.sha256(context_id.encode("utf-8", errors="replace")).hexdigest()[:24]
    workspace = work_root / workspace_key
    if workspace.exists():
        shutil.rmtree(workspace)
    input_root = workspace / "input"
    attachment_root = workspace / "attachments"
    output_root = workspace / "generated"
    input_root.mkdir(parents=True)
    attachment_root.mkdir(parents=True)
    output_root.mkdir(parents=True)

    text_chunks: list[str] = []
    task_id: str | None = None
    names: list[str] = []
    for wrapped in message.parts:
        part: Any = wrapped.root
        if isinstance(part, TextPart):
            text_chunks.append(part.text)
        elif isinstance(part, DataPart):
            candidate = part.data.get("task_id")
            if isinstance(candidate, str) and candidate.strip():
                task_id = candidate.strip()
        elif isinstance(part, FilePart) and isinstance(part.file, FileWithBytes):
            name = _safe_filename(part.file.name)
            try:
                raw = base64.b64decode(part.file.bytes, validate=True)
            except Exception as exc:
                raise ValueError(f"attachment {name!r} is not valid base64") from exc
            if len(raw) > max_attachment_bytes:
                raise ValueError(f"attachment {name!r} exceeds HARNESS_MAX_ATTACHMENT_BYTES")
            (attachment_root / name).write_bytes(raw)
            names.append(name)
            if name == "repo-vul.tar.gz":
                _safe_extract_tar(raw, input_root / "repo-vul", max_extracted_bytes)
            elif name == "repo-fix.tar.gz":
                _safe_extract_tar(raw, input_root / "repo-fix", max_extracted_bytes)
            elif name.endswith((".txt", ".md", ".diff")):
                (input_root / name).write_bytes(raw)

    if not names:
        raise ValueError("no FileWithBytes challenge attachments received")
    fallback = f"a2a:{context_id}"
    return Challenge(
        task_id=task_id or fallback,
        task_id_source="message_data" if task_id else "a2a_context_fallback",
        prompt="\n".join(text_chunks),
        difficulty=_difficulty(set(names)),
        workspace=workspace,
        input_root=input_root,
        attachment_names=sorted(names),
    )


def first_data_part(message: Message) -> dict[str, Any] | None:
    for wrapped in message.parts:
        if isinstance(wrapped.root, DataPart):
            return wrapped.root.data
    return None

