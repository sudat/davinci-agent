# noqa: INP001 (evidence tree is not an importable package by design)
"""Fabricated-row selfcheck for the Task 2 quality measurement modules.

ASCII-only synthetic data (no prose, no absolute-path literals, no CJK)
exercises every pure rule: frozen conversion with timeline-start
subtraction BEFORE ms, half-open/overlap/bool/negative refusals, r1/r2
stability, hash checks, injected-limit labels + production-source identity,
gate decision, diagnosis counts, allowlist."""

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

import caption_logic as caption  # noqa: E402 (path bootstrap first — task4 pattern)
import quality_logic as qlogic  # noqa: E402 (path bootstrap first)
import quality_wiring as wiring  # noqa: E402 (services stack for source identity)

from services.cli import _v44_arm_transcript as arm  # noqa: E402
from services.cli.v44_arm_stages import ArmPipelineError  # noqa: E402
from services.metrics.v44_product_proof import EvidenceQualityMetrics  # noqa: E402


class ExpectFn(Protocol):
    def __call__(self, label: str, *, passed: bool) -> None: ...


def _ws_normalize(text: str) -> str:
    return "".join(text.split())


def _refused(
    fn: Callable[[], object], error_type: type[Exception], reason: str | None = None,
) -> bool:
    """True iff ``fn`` raised ``error_type`` (matching ``reason`` when given)."""
    try:
        fn()
    except error_type as error:
        return reason is None or getattr(error, "reason", None) == reason
    print(f"[FAIL] selfcheck: expected {error_type.__name__} refusal", flush=True)
    return False


def _quality_refused(fn: Callable[[], object], reason: str) -> bool:
    return _refused(fn, qlogic.QualityRefusalError, reason)


def _check_conversion(expect: ExpectFn) -> None:
    bool_start = True
    expected_start_ms = 1969
    expected_end_ms = 2736
    triples = qlogic.convert_cues([("synth-a", 108059, 108082)], 108000)
    expect("absolute start subtracted before ms conversion",
           passed=triples == [(expected_start_ms, expected_end_ms, "synth-a")])
    naive = round(108059 * 1001 / 30)
    zero_ms = 0
    expect("conversion differs from naive absolute; timeline start -> 0 ms",
           passed=triples[0][0] != naive and qlogic.relative_ms(108000, 108000) == zero_ms)
    half_even_down = 500
    half_even_up = 1502
    expect("frozen rounding is deterministic half-even",
           passed=(qlogic.relative_ms(15, 0) == half_even_down
                   and qlogic.relative_ms(45, 0) == half_even_up))
    expect("negative relative frame and bool timeline start refused",
           passed=(_quality_refused(
               lambda: qlogic.relative_ms(107999, 108000), "negative-relative-frame")
               and _quality_refused(
                   lambda: qlogic.convert_cues([("synth-a", 1, 2)], bool_start),
                   "timeline-start-invalid")))


def _check_readback_refusals(expect: ExpectFn) -> None:
    for label, rows in (
        ("reversed span", [{"text": "row-a", "start": 9, "end": 9}]),
        ("overlapping spans", [{"text": "row-a", "start": 0, "end": 30},
                               {"text": "row-b", "start": 20, "end": 40}]),
        ("bool frame", [{"text": "row-a", "start": True, "end": 5}]),
    ):
        expect(f"readback {label} refused",
               passed=_refused(lambda rows=rows: caption.canonical_cues(rows),
                               caption.ProbeRefusalError))


def _check_stability_and_hash(expect: ExpectFn) -> None:
    left = [(0, 33, "row-a")]
    qlogic.stability_check(left, [(0, 33, "row-a")])
    expect("identical converted readbacks accepted", passed=True)
    expect("r1/r2 disagreement refused",
           passed=_quality_refused(
               lambda: qlogic.stability_check(left, [(0, 66, "row-a")]),
               "stability-violation"))
    qlogic.verify_hash("a" * 64, "a" * 64, "synthetic")
    expect("matching canonical hash accepted", passed=True)
    expect("canonical hash mismatch refused",
           passed=_quality_refused(
               lambda: qlogic.verify_hash("a" * 64, "b" * 64, "synthetic"),
               "hash-mismatch"))
    expect("empty expected hash refused",
           passed=_quality_refused(
               lambda: qlogic.verify_hash("a" * 64, "", "synthetic"), "hash-mismatch"))


def _check_threshold_labels(expect: ExpectFn) -> None:
    limits = qlogic.ThresholdLimits(
        cer_max=0.5, timestamp_p95_ms_max=100.0, omitted_max=2, duplicated_max=2)
    within = qlogic.gate_checks(
        qlogic.MetricValues(cer=0.2, p95_ms=50.0, omitted=1, duplicated=1), limits)
    boundary = qlogic.gate_checks(
        qlogic.MetricValues(cer=0.5, p95_ms=100.0, omitted=2, duplicated=2), limits)
    exceeding = qlogic.gate_checks(
        qlogic.MetricValues(cer=0.6, p95_ms=150.0, omitted=3, duplicated=3), limits)
    expect("labels derive from injected limits (within incl. boundary)",
           passed=(set(within.values()) == {"pass"}
                   and set(boundary.values()) == {"pass"}
                   and set(exceeding.values()) == {"fail"}))
    for label, values in (
        ("null cer", qlogic.MetricValues(cer=None, p95_ms=1.0, omitted=1, duplicated=1)),
        ("null p95", qlogic.MetricValues(cer=0.1, p95_ms=None, omitted=1, duplicated=1)),
        ("null omitted", qlogic.MetricValues(cer=0.1, p95_ms=1.0, omitted=None, duplicated=1)),
        ("null duplicated", qlogic.MetricValues(cer=0.1, p95_ms=1.0, omitted=1, duplicated=None)),
    ):
        expect(f"{label} refused (fail-closed)",
               passed=_quality_refused(
                   lambda values=values: qlogic.gate_checks(values, limits),
                   "metrics-null"))


def _check_threshold_source_and_decision(expect: ExpectFn) -> None:
    expect("threshold constants are the production module's objects",
           passed=(wiring.CER_MAX is arm.CER_MAX
                   and wiring.TIMESTAMP_P95_MS_MAX is arm.TIMESTAMP_P95_MS_MAX
                   and wiring.OMITTED_MAX is arm.OMITTED_MAX
                   and wiring.DUPLICATED_MAX is arm.DUPLICATED_MAX))
    expect("wired limits equal the production constants",
           passed=qlogic.ThresholdLimits(
               cer_max=arm.CER_MAX, timestamp_p95_ms_max=arm.TIMESTAMP_P95_MS_MAX,
               omitted_max=arm.OMITTED_MAX, duplicated_max=arm.DUPLICATED_MAX)
           == wiring.THRESHOLD_LIMITS)
    failing = EvidenceQualityMetrics(transcript_cer=0.5, timestamp_error_p95_ms=6000.0,
                                     omitted_utterances=31, duplicated_utterances=37)
    labels_fail, outcome_fail = wiring.evaluate_thresholds(failing)
    expect("production gate refuses exceeding metrics",
           passed=(outcome_fail == "asr-alignment-failed"
                   and set(labels_fail.values()) == {"fail"}))
    passing = EvidenceQualityMetrics(transcript_cer=0.05, timestamp_error_p95_ms=100.0,
                                     omitted_utterances=1, duplicated_utterances=1)
    labels_ok, outcome_ok = wiring.evaluate_thresholds(passing)
    expect("production gate accepts within-limit metrics",
           passed=(outcome_ok == "thresholds-passed"
                   and set(labels_ok.values()) == {"pass"}))

    def raising_enforce(_metrics: object) -> None:
        raise ArmPipelineError("asr-alignment-failed", "synthetic")

    expect("decision honors a refusing enforce seam",
           passed=wiring.evaluate_thresholds(passing, raising_enforce)[1]
           == "asr-alignment-failed")
    expect("decision honors an accepting enforce seam",
           passed=wiring.evaluate_thresholds(passing, lambda _metrics: None)[1]
           == "thresholds-passed")


def _check_diagnosis(expect: ExpectFn) -> None:
    reference = [
        (0, 1000, "hello world"), (2000, 3000, "second utterance here"),
        (100000, 101000, "faraway")]
    hypothesis = [
        (10, 500, "hello"), (500, 990, "world"), (1000, 1500, "second"),
        (1600, 2100, "utterance"), (2200, 2600, "second"), (3000, 3050, "far"),
        (3050, 3100, "away"), (3200, 3300, "zzz")]
    counts = qlogic.diagnose_segmentation(hypothesis, reference, _ws_normalize)
    omitted_total = 3
    merge_split = 1
    actual_missing = 2
    duplicated_total = 8
    segmentation_split = 7
    actual_duplication = 1
    expect("omission split into merge/split vs actual missing",
           passed=(counts["omitted_total"] == omitted_total
                   and counts["segmentation_merge_split"] == merge_split
                   and counts["actual_missing"] == actual_missing))
    expect("duplication split into segmentation vs actual",
           passed=(counts["duplicated_total"] == duplicated_total
                   and counts["segmentation_split"] == segmentation_split
                   and counts["actual_duplication"] == actual_duplication))


def _check_allowlist(expect: ExpectFn) -> None:
    good = {"schema_version": "resolve-auto-caption-quality-v1",
            "provider": "resolve-auto-caption", "runs": ["r1", "r2"],
            "decision": "thresholds-passed", "checks": {"transcript_cer": "pass"},
            "source": "services.cli._v44_arm_transcript",
            "r1_canonical_sha256": "a" * 64, "pin_commit": "b" * 40,
            "resolve_version": "21.0.4.5", "count": 3, "ok": True}
    try:
        qlogic.assert_quality_sanitized(good)
        expect("sanitized quality report passes allowlist", passed=True)
    except qlogic.QualityRefusalError:
        expect("sanitized quality report passes allowlist", passed=False)
    for label, bad in (
        ("caption prose refused", {**good, "verdict": "synthetic caption prose refused"}),
        ("absolute path refused", {**good, "note": str(PurePosixPath(
            "/", "Users", "x", "private", "media.mov"))}),
        ("free-form error refused", {**good, "err": "boom at file /tmp/x"}),
        ("bad key refused", {**good, "BadKey": 1}),
    ):
        expect(f"allowlist {label}",
               passed=_quality_refused(
                   lambda bad=bad: qlogic.assert_quality_sanitized(bad),
                   "unsanitized-output"))


def run_selfcheck() -> int:
    """Run every fabricated-row check; 0 iff all checks pass."""
    failures: list[str] = []

    def expect(label: str, *, passed: bool) -> None:
        print(f"[{'ok' if passed else 'FAIL'}] selfcheck: {label}", flush=True)
        if not passed:
            failures.append(label)

    _check_conversion(expect)
    _check_readback_refusals(expect)
    _check_stability_and_hash(expect)
    _check_threshold_labels(expect)
    _check_threshold_source_and_decision(expect)
    _check_diagnosis(expect)
    _check_allowlist(expect)
    try:
        qlogic.QualityRefusalError("not-a-label")
    except ValueError:
        closed = True
    else:
        closed = False
    expect("refusal reason labels closed", passed=closed)
    all_passed = not failures
    print(f"selfcheck: {'PASS' if all_passed else f'FAIL ({len(failures)})'}",
          flush=True)
    return 0 if all_passed else 1


__all__ = ["run_selfcheck"]
