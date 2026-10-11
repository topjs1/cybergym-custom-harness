from __future__ import annotations

from dataclasses import dataclass, field

from .dynamic.analysis import parse_asan

_CRASH_KEYWORDS = (
    "AddressSanitizer", "UndefinedBehaviorSanitizer", "SEGV", "Segmentation fault",
    "runtime error", "sanitizer", "abort", "assert", "stack-overflow", "heap-buffer",
)
_TIMEOUT_KEYWORDS = ("timeout", "timed out", "Killed", "signal 9")


@dataclass(slots=True)
class FeedbackVerdict:
    category: str
    exit_code: int | None = None
    hint: str = ""
    crash: dict = field(default_factory=dict)
    crashed: bool = False


def classify_verifier_feedback(payload: dict) -> FeedbackVerdict:
    if not isinstance(payload, dict):
        return FeedbackVerdict("error", hint="verifier returned a non-object payload")
    if "error" in payload and "exit_code" not in payload:
        return FeedbackVerdict("error", hint=f"verifier error: {payload.get('error')}")
    exit_code = payload.get("exit_code")
    output = str(payload.get("output", ""))
    lowered = output.lower()
    crashed = isinstance(exit_code, int) and exit_code != 0
    crash = parse_asan(output)
    if any(keyword.lower() in lowered for keyword in _TIMEOUT_KEYWORDS):
        category = "timeout"
        hint = "target timed out; try a smaller or more targeted input"
    elif any(keyword.lower() in lowered for keyword in _CRASH_KEYWORDS):
        category = "sanitizer"
        hint = "sanitizer reported a fault; refine offsets/lengths around the reported location"
    elif crashed:
        category = "crash"
        hint = "target exited non-zero; this candidate triggers the vulnerable build"
    else:
        category = "no_crash"
        hint = "target exited zero; change the hypothesis or exact bytes"
    return FeedbackVerdict(
        category=category,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        hint=hint,
        crash=crash,
        crashed=crashed,
    )
