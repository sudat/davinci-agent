# noqa: INP001 (evidence tree is not an importable package by design)
"""Synthetic allowlist selfcheck for committed quality/summary outputs.

ASCII-only fabricated payloads (no prose, no absolute-path literals, no
CJK) exercise the committed-output allowlist end to end: a sanitized
quality report and a sanitized spike summary pass, while caption prose,
absolute private paths, free-form errors, bad keys, and free-form summary
status/reason values are refused with ``unsanitized-output``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Protocol

EVIDENCE = Path(__file__).resolve().parent
VIDEO_PIPELINE = EVIDENCE.parents[3]
for _path in (VIDEO_PIPELINE, EVIDENCE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import quality_logic as qlogic  # noqa: E402 (path bootstrap first, task4 pattern)


class ExpectFn(Protocol):
    def __call__(self, label: str, *, passed: bool) -> None: ...


def _quality_refused(fn: Callable[[], object], reason: str) -> bool:
    try:
        fn()
    except qlogic.QualityRefusalError as error:
        return error.reason == reason
    print("[FAIL] selfcheck: expected QualityRefusalError refusal", flush=True)
    return False


def _report_fixture() -> dict[str, object]:
    return {"schema_version": "resolve-auto-caption-quality-v1",
            "provider": "resolve-auto-caption", "runs": ["r1", "r2"],
            "decision": "thresholds-passed", "checks": {"transcript_cer": "pass"},
            "source": "services.cli._v44_arm_transcript",
            "r1_canonical_sha256": "a" * 64, "pin_commit": "b" * 40,
            "resolve_version": "21.0.4.5", "count": 3, "ok": True}


def _summary_fixture() -> dict[str, object]:
    return {"schema_version": "resolve-auto-caption-summary-v1",
            "provider": "resolve-auto-caption",
            "integration_status": "blocked-with-reason",
            "blocked_reason": "blocked-quality",
            "gate_v44_2_passed": False,
            "product_integration_provider_call_count": 0}


def run_checks(expect: ExpectFn) -> None:
    """Allowlist pass/refusal checks for report and spike-summary payloads."""
    good = _report_fixture()
    try:
        qlogic.assert_quality_sanitized(good)
        expect("sanitized quality report passes allowlist", passed=True)
    except qlogic.QualityRefusalError:
        expect("sanitized quality report passes allowlist", passed=False)
    summary = _summary_fixture()
    try:
        qlogic.assert_quality_sanitized(summary)
        expect("sanitized spike summary passes allowlist", passed=True)
    except qlogic.QualityRefusalError:
        expect("sanitized spike summary passes allowlist", passed=False)
    for label, bad in (
        ("caption prose refused", {**good, "verdict": "synthetic caption prose refused"}),
        ("absolute path refused", {**good, "note": str(PurePosixPath(
            "/", "Users", "x", "private", "media.mov"))}),
        ("free-form error refused", {**good, "err": "boom at file /tmp/x"}),
        ("bad key refused", {**good, "BadKey": 1}),
        ("summary free-form status refused",
         {**summary, "integration_status": "blocked with reason"}),
        ("summary free-form reason refused",
         {**summary, "blocked_reason": "quality was poor"}),
    ):
        expect(f"allowlist {label}",
               passed=_quality_refused(
                   lambda bad=bad: qlogic.assert_quality_sanitized(bad),
                   "unsanitized-output"))


__all__ = ["run_checks"]
