from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".rs", ".go", ".java", ".py", ".js", ".ts", ".proto", ".inc",
    ".cmake", ".mk", ".toml", ".yaml", ".yml", ".json", ".xml",
}
SKIP_DIRS = {".git", ".svn", "node_modules", "target", "build", "dist", "vendor"}


def _inside(root: Path, relative: str) -> Path:
    if not relative or "\x00" in relative:
        raise ValueError("path must be a non-empty relative path")
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError("path escapes the task workspace")
    return candidate


@dataclass(slots=True)
class CodeTools:
    root: Path
    output_limit: int = 12_000

    def list_files(self, subdir: str = ".", limit: int = 300) -> str:
        base = _inside(self.root, subdir)
        if not base.exists() or not base.is_dir():
            return f"error: directory not found: {subdir}"
        rows: list[str] = []
        for current, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                path = Path(current) / name
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                rows.append(f"{path.relative_to(self.root)}\t{size}")
                if len(rows) >= min(max(limit, 1), 1000):
                    rows.append("[truncated]")
                    return "\n".join(rows)
        return "\n".join(rows) if rows else "(no files)"

    def read_file(self, path: str, start_line: int = 1, end_line: int = 240) -> str:
        target = _inside(self.root, path)
        if not target.is_file():
            return f"error: file not found: {path}"
        start = max(1, start_line)
        end = min(max(start, end_line), start + 399)
        size = target.stat().st_size
        with target.open("rb") as binary:
            if b"\x00" in binary.read(4096):
                return f"binary file: {path} ({size} bytes)"
        rendered: list[str] = []
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle, 1):
                if index > end:
                    break
                if index >= start:
                    rendered.append(f"{index:6d}  {line.rstrip()[:4000]}")
                if sum(len(item) for item in rendered) >= self.output_limit:
                    rendered.append("[truncated]")
                    break
        result = "\n".join(rendered) or "(empty range)"
        return result[: self.output_limit]

    async def search_code(self, pattern: str, subdir: str = ".", limit: int = 80) -> str:
        if not pattern or len(pattern) > 300:
            return "error: pattern length must be 1..300"
        base = _inside(self.root, subdir)
        cap = min(max(limit, 1), 200)
        rg = shutil.which("rg")
        if rg:
            proc = await asyncio.create_subprocess_exec(
                rg,
                "--line-number",
                "--no-heading",
                "--color=never",
                "--glob=!.git/**",
                "--glob=!node_modules/**",
                "--glob=!build/**",
                "--glob=!vendor/**",
                "--max-count",
                str(cap),
                pattern,
                str(base),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8)
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return "error: search timed out"
            if proc.returncode not in {0, 1}:
                return f"error: rg failed: {stderr.decode(errors='replace')[:1000]}"
            output = stdout.decode("utf-8", errors="replace")
            output = output.replace(str(self.root) + "/", "")
            return output[: self.output_limit] or "(no matches)"
        return await asyncio.to_thread(self._python_search, pattern, base, cap)

    def _python_search(self, pattern: str, base: Path, limit: int) -> str:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"error: invalid regex: {exc}"
        hits: list[str] = []
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                path = Path(current) / name
                if path.suffix.lower() not in SOURCE_SUFFIXES and name not in {"Makefile", "CMakeLists.txt"}:
                    continue
                try:
                    if path.stat().st_size > 8 * 1024 * 1024:
                        continue
                    lines = path.read_text(errors="replace").splitlines()
                except OSError:
                    continue
                for number, line in enumerate(lines, 1):
                    if compiled.search(line):
                        hits.append(f"{path.relative_to(self.root)}:{number}:{line[:300]}")
                        if len(hits) >= limit:
                            return "\n".join(hits) + "\n[truncated]"
        return "\n".join(hits) if hits else "(no matches)"

    async def symbol_context(self, symbol: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:~]*", symbol):
            return "error: invalid symbol name"
        matches = await self.search_code(rf"\b{re.escape(symbol)}\s*\(", limit=20)
        if matches.startswith(("error:", "(no matches)")):
            return matches
        chunks: list[str] = []
        seen: set[tuple[str, int]] = set()
        for row in matches.splitlines():
            parsed = re.match(r"(.+?):(\d+):", row)
            if not parsed:
                continue
            path, line_text = parsed.groups()
            line = int(line_text)
            key = (path, line)
            if key in seen:
                continue
            seen.add(key)
            chunks.append(f"=== {path}:{line} ===\n{self.read_file(path, max(1, line - 20), line + 80)}")
            if len(chunks) == 3:
                break
        return "\n\n".join(chunks)[: self.output_limit]

    def analyze_error(self, path: str = "error.txt") -> str:
        target = _inside(self.root, path)
        if not target.is_file():
            return f"error: file not found: {path}"
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(2 * 1024 * 1024)
        sanitizer = re.search(r"ERROR: (?:AddressSanitizer|UndefinedBehaviorSanitizer): ([^\n]+)", text)
        signal = re.search(r"(?:AddressSanitizer:DEADLYSIGNAL|received signal|signal)\s*([^\n]*)", text, re.I)
        frames = re.findall(r"^\s*#\d+\s+[^\n]*", text, re.M)[:12]
        locations = re.findall(r"(?:[A-Za-z0-9_./-]+\.(?:c|cc|cpp|h|rs|go|java)):\d+(?::\d+)?", text)[:12]
        result: dict[str, Any] = {
            "sanitizer": sanitizer.group(1).strip() if sanitizer else None,
            "signal": signal.group(1).strip() if signal else None,
            "locations": locations,
            "frames": frames,
        }
        return repr(result)[: self.output_limit]

