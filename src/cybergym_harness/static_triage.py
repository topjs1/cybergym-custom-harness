from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .code_tools import SKIP_DIRS, SOURCE_SUFFIXES
from .dynamic.analysis import parse_asan

_ENTRY_PATTERNS = ("LLVMFuzzerTestOneInput", "int main", "fuzz_target", "FuzzerTestOneInput")
_SINK_KEYWORDS = (
    "memcpy", "memmove", "strcpy", "strncpy", "strcat", "sprintf", "snprintf",
    "malloc", "realloc", "calloc", "free", "read", "fread", "alloca", "index",
    "parse", "decode", "copy", "length", "size", "offset", "strlen",
)
_DOMAIN_KEYWORDS = {
    "image": ("png", "jpeg", "jpg", "gif", "bmp", "tiff", "webp", "image"),
    "audio": ("mp3", "wav", "flac", "ogg", "audio", "sound"),
    "font": ("font", "truetype", "ttf", "otf", "freetype", "woff"),
    "compression": ("zip", "gzip", "zlib", "bzip", "lzma", "archive", "decompress"),
    "crypto": ("crypto", "x509", "asn1", "certificate", "rsa", "aes", "tls"),
    "pdf": ("pdf", "poppler", "postscript"),
    "network": ("packet", "socket", "tcp", "udp", "dns", "http"),
    "xml": ("xml", "html", "sgml", "expat"),
    "serialization": ("json", "protobuf", "msgpack", "cbor", "serialize", "bson"),
    "regex": ("regex", "pcre", "re2", "pattern"),
}
_MAX_FILES = 4000
_MAX_FILE_BYTES = 2 * 1024 * 1024
_BUILD_MARKERS = (
    "CMakeLists.txt", "Makefile", "makefile", "GNUmakefile", "configure", "configure.ac",
    "meson.build", "BUILD", "BUILD.bazel", "build.sh", "Cargo.toml", "go.mod", "setup.py",
    "pyproject.toml", "SConstruct", "wscript",
)


@dataclass(slots=True)
class TriageResult:
    ranked_files: list[tuple[str, int]] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    fuzz_target: str | None = None
    sanitizer: str | None = None
    crash_function: str | None = None
    crash_locations: list[str] = field(default_factory=list)
    input_kind: str = "unknown"
    domain: str | None = None
    magic: list[str] = field(default_factory=list)
    build_files: list[str] = field(default_factory=list)
    tree: list[str] = field(default_factory=list)

    def render(self, limit: int = 8) -> str:
        lines = ["Static triage (deterministic, no LLM):"]
        if self.entry_points:
            lines.append(f"- fuzz/entry points: {', '.join(self.entry_points[:4])}")
        if self.fuzz_target:
            lines.append(f"- likely fuzz target: {self.fuzz_target}")
        if self.sanitizer or self.crash_function:
            lines.append(f"- crash hint: {self.sanitizer or 'unknown'} in {self.crash_function or 'unknown'}")
        if self.crash_locations:
            lines.append(f"- crash locations: {', '.join(self.crash_locations[:5])}")
        lines.append(f"- input kind: {self.input_kind}")
        if self.domain:
            lines.append(f"- project domain: {self.domain}")
        if self.magic:
            lines.append(f"- magic bytes: {', '.join(self.magic[:6])}")
        if self.ranked_files:
            rendered = ", ".join(f"{path}({score})" for path, score in self.ranked_files[:limit])
            lines.append(f"- ranked files: {rendered}")
        if self.build_files:
            lines.append(f"- build system files: {', '.join(self.build_files[:8])}")
        if self.tree:
            lines.append("- directory tree (bounded; use this instead of repeated ls):")
            lines.extend(f"    {line}" for line in self.tree[:40])
        return "\n".join(lines)


def _iter_source_files(repo: Path):
    count = 0
    for current, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = Path(current) / name
            if path.suffix.lower() not in SOURCE_SUFFIXES and name not in {"Makefile", "CMakeLists.txt"}:
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            count += 1
            if count > _MAX_FILES:
                return
            yield path


def static_triage(repo: Path, description_text: str = "", error_text: str = "") -> TriageResult:
    result = TriageResult()
    combined = f"{description_text}\n{error_text}"
    crashed = parse_asan(error_text) if error_text else {}
    result.sanitizer = crashed.get("sanitizer_error") if isinstance(crashed, dict) else None
    result.crash_function = crashed.get("crash_function") if isinstance(crashed, dict) else None
    result.crash_locations = list(crashed.get("locations", [])) if isinstance(crashed, dict) else []
    result.input_kind = _guess_input_kind(combined)
    result.domain = _guess_domain(combined)
    if not repo.is_dir():
        return result

    keyword_terms = _keywords(combined)
    function_hint = (result.crash_function or "").split()[0].strip("()") if result.crash_function else ""
    scored: list[tuple[int, str]] = []
    for path in _iter_source_files(repo):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        relative = str(path.relative_to(repo))
        score = 0
        for marker in _ENTRY_PATTERNS:
            if marker in text:
                score += 6
                if marker in {"LLVMFuzzerTestOneInput", "FuzzerTestOneInput"}:
                    result.fuzz_target = result.fuzz_target or relative
                if relative not in result.entry_points:
                    result.entry_points.append(relative)
                break
        if function_hint and re.search(rf"\b{re.escape(function_hint)}\b", text):
            score += 8
        for term in keyword_terms:
            if term in text:
                score += 2
        lowered = text.lower()
        for sink in _SINK_KEYWORDS:
            if sink in lowered:
                score += 1
        if path.suffix.lower() in {".c", ".cc", ".cpp", ".cxx"}:
            score += 1
        if score > 0:
            scored.append((score, relative))
        if function_hint:
            for match in re.finditer(rf"\b{re.escape(function_hint)}\b", text):
                snippet_start = max(0, match.start() - 24)
                window = text[snippet_start:match.start() + 24]
                for magic in _magic_bytes(window):
                    if magic not in result.magic:
                        result.magic.append(magic)
    scored.sort(key=lambda item: (-item[0], item[1]))
    result.ranked_files = [(path, score) for score, path in scored[:20]]
    result.magic = result.magic[:8]
    result.build_files = _detect_build_files(repo)
    result.tree = _tree_lines(repo)
    return result


def _detect_build_files(repo: Path) -> list[str]:
    found: list[str] = []
    for current, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name in _BUILD_MARKERS:
                path = Path(current) / name
                found.append(str(path.relative_to(repo)))
                if len(found) >= 12:
                    return found
    return found


def _tree_lines(root: Path, max_lines: int = 40, max_depth: int = 3, per_dir: int = 8) -> list[str]:
    lines: list[str] = []

    def recurse(path: Path, prefix: str, depth: int) -> None:
        if depth > max_depth or len(lines) >= max_lines:
            return
        try:
            entries = sorted(os.scandir(path), key=lambda entry: (not entry.is_dir(), entry.name))
        except OSError:
            return
        dirs = [entry for entry in entries if entry.is_dir() and entry.name not in SKIP_DIRS]
        files = [entry for entry in entries if entry.is_file()]
        for entry in dirs[:per_dir]:
            lines.append(f"{prefix}{entry.name}/")
            if len(lines) >= max_lines:
                return
            recurse(Path(entry.path), prefix + "  ", depth + 1)
        if len(dirs) > per_dir:
            lines.append(f"{prefix}... (+{len(dirs) - per_dir} dirs)")
        for entry in files[:per_dir]:
            lines.append(f"{prefix}{entry.name}")
            if len(lines) >= max_lines:
                return
        if len(files) > per_dir:
            lines.append(f"{prefix}... (+{len(files) - per_dir} files)")

    if root.is_dir():
        recurse(root, "", 1)
    return lines[:max_lines]


def _keywords(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text)
    stop = {"that", "with", "this", "from", "have", "when", "which", "using", "into", "over"}
    seen: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered in stop or token in seen:
            continue
        seen.append(token)
        if len(seen) >= 40:
            break
    return seen


def _guess_input_kind(text: str) -> str:
    lowered = text.lower()
    if "command" in lowered and "arg" in lowered:
        return "command_arg"
    if any(word in lowered for word in ("text file", "textfile", "parse text", "ascii")):
        return "text_file"
    if any(word in lowered for word in ("binary", "byte", "buffer", "file")):
        return "binary_file"
    return "unknown"


def _guess_domain(text: str) -> str | None:
    lowered = text.lower()
    for domain, markers in _DOMAIN_KEYWORDS.items():
        if any(marker in lowered for marker in markers):
            return domain
    return None


def _magic_bytes(window: str) -> list[str]:
    magics: list[str] = []
    for match in re.finditer(r"(0x[0-9a-fA-F]{2})", window):
        magics.append(match.group(1).lower())
    for match in re.finditer(r"'([ -~])'", window):
        magics.append(f"0x{ord(match.group(1)):02x}")
    return magics
