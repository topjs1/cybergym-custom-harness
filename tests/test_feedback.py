from __future__ import annotations

from cybergym_harness.feedback import classify_verifier_feedback


def test_success_on_nonzero_exit():
    verdict = classify_verifier_feedback({"exit_code": 1, "output": ""})
    assert verdict.crashed is True
    assert verdict.category == "crash"


def test_sanitizer_detected_on_zero_exit():
    verdict = classify_verifier_feedback({
        "exit_code": 0,
        "output": "ERROR: AddressSanitizer: heap-buffer-overflow\n#0 0x1 in parse /src/p.c:3",
    })
    assert verdict.crashed is False
    assert verdict.category == "sanitizer"
    assert verdict.crash["crash_function"].startswith("parse")


def test_no_crash_and_error_payloads():
    assert classify_verifier_feedback({"exit_code": 0, "output": "ok"}).category == "no_crash"
    assert classify_verifier_feedback({"error": "boom"}).category == "error"
    assert classify_verifier_feedback("nope").category == "error"


def test_timeout_detection():
    verdict = classify_verifier_feedback({"exit_code": 0, "output": "execution timed out"})
    assert verdict.category == "timeout"
