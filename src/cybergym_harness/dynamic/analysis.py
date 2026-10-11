from __future__ import annotations

import re

_SANITIZER = re.compile(
    r"ERROR: (?:AddressSanitizer|UndefinedBehaviorSanitizer|MemorySanitizer|ThreadSanitizer|LeakSanitizer): ([^\n]+)"
)
_FRAME = re.compile(r"^\s*#\d+\s+0x[0-9a-fA-F]+\s+in\s+([^\n]+)", re.M)
_LOCATION = re.compile(r"([A-Za-z0-9_./-]+\.(?:c|cc|cpp|cxx|h|hh|hpp|rs|go|java)):(\d+)(?::\d+)?")
_SUMMARY = re.compile(r"SUMMARY: (?:AddressSanitizer|UndefinedBehaviorSanitizer): ([^\n]+)")


def parse_asan(text: str) -> dict[str, object]:
    frames = [match.strip() for match in _FRAME.findall(text)[:8]]
    locations = [f"{path}:{line}" for path, line in _LOCATION.findall(text)[:8]]
    summary = _SUMMARY.search(text)
    error = _SANITIZER.search(text)
    return {
        "sanitizer_error": (error.group(1).strip() if error else None),
        "summary": (summary.group(1).strip() if summary else None),
        "crash_function": (frames[0] if frames else None),
        "frames": frames,
        "locations": locations,
    }
