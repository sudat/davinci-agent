# noqa: INP001 (evidence tree is not an importable package by design)
"""Pure rules for the Task 2 quality measurement (no services imports).

Owns the plan-frozen frame->ms conversion (subtract the recorded timeline
start FIRST, then ``round(relative * 1001 / 30)``), r1/r2 stability,
threshold labels over INJECTED limits (production constants wired in
``quality_wiring``, never redeclared here), the deterministic segmentation
diagnosis (counts only), and the committed-output allowlist.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from typing import Final, NamedTuple

from caption_logic import FIELD_KEY_RE, Cue, string_allowed

#: One hypothesis/reference row: ms half-open ``[start, end)`` + exact text.
type MsTriple = tuple[int, int, str]

#: Text normalization injected by the wiring (the matching rule's own).
type TextNormalizer = Callable[[str], str]

#: Closed refusal labels for the quality measurement path.
REFUSAL_LABELS: Final = frozenset({
    "readback-missing", "readback-unreadable", "readback-invalid",
    "negative-relative-frame", "timeline-start-invalid", "timeline-start-mismatch",
    "hash-mismatch", "capability-mismatch", "input-mismatch", "stability-violation",
    "metrics-null", "diagnosis-inconsistent", "write-error", "unsanitized-output",
})

#: Exact strings allowed in committed quality outputs beyond Task 1's
#: allowlist (labels, provider id, schema ids, threshold source module,
#: and the Task 5 spike-summary closure labels).
QUALITY_LABELS: Final = frozenset({
    "thresholds-passed", "asr-alignment-failed", "pass", "fail", "r1", "r2",
    "resolve-auto-caption", "resolve-auto-caption-quality-v1",
    "services.cli._v44_arm_transcript",
    "blocked-with-reason", "blocked-quality",
})


class QualityRefusalError(Exception):
    """Typed refusal: closed-enumeration reason label + counts-only detail."""

    def __init__(self, reason: str, detail: str = "") -> None:
        if reason not in REFUSAL_LABELS:
            msg = f"unknown refusal reason label: {reason!r}"
            raise ValueError(msg)
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class MetricValues(NamedTuple):
    """The four gate metrics exactly as ``EvidenceQualityMetrics`` carries."""

    cer: float | None
    p95_ms: float | None
    omitted: int | None
    duplicated: int | None


class ThresholdLimits(NamedTuple):
    """The four frozen limits (values imported from the production module)."""

    cer_max: float
    timestamp_p95_ms_max: float
    omitted_max: int
    duplicated_max: int


def gate_checks(values: MetricValues, limits: ThresholdLimits) -> dict[str, str]:
    """Per-metric pass/fail labels; any null metric is a typed refusal."""
    if (values.cer is None or values.p95_ms is None
            or values.omitted is None or values.duplicated is None):
        raise QualityRefusalError("metrics-null", "a gate metric is null (fail-closed)")
    return {
        "transcript_cer": "pass" if values.cer <= limits.cer_max else "fail",
        "timestamp_error_p95_ms":
            "pass" if values.p95_ms <= limits.timestamp_p95_ms_max else "fail",
        "omitted_utterances":
            "pass" if values.omitted <= limits.omitted_max else "fail",
        "duplicated_utterances":
            "pass" if values.duplicated <= limits.duplicated_max else "fail",
    }


def relative_ms(absolute_frame: int, timeline_start_frame: int) -> int:
    """Plan-frozen conversion: subtract the timeline start FIRST, then ms."""
    relative = absolute_frame - timeline_start_frame
    if relative < 0:
        raise QualityRefusalError(
            "negative-relative-frame",
            f"absolute frame {absolute_frame} precedes timeline start "
            f"{timeline_start_frame}",
        )
    return round(relative * 1001 / 30)


def convert_cues(cues: Sequence[Cue], timeline_start_frame: int) -> list[MsTriple]:
    """Validated absolute-frame cues -> ms triples (end>start holds: one
    frame gap converts to >=33 ms; span validity was proven at frame level)."""
    if not (isinstance(timeline_start_frame, int)
            and not isinstance(timeline_start_frame, bool)):
        raise QualityRefusalError(
            "timeline-start-invalid", "timeline start frame not an integer")
    return [
        (relative_ms(start, timeline_start_frame),
         relative_ms(end, timeline_start_frame), text)
        for text, start, end in cues
    ]


def stability_check(r1: Sequence[MsTriple], r2: Sequence[MsTriple]) -> None:
    """Refuse unless the two converted readbacks are exactly equal."""
    if list(r1) != list(r2):
        raise QualityRefusalError(
            "stability-violation", "r1/r2 converted readbacks differ")


def verify_hash(computed: str, expected: str, source: str) -> None:
    """Refuse on canonical-sha mismatch against one recorded source."""
    if not expected or computed != expected:
        raise QualityRefusalError(
            "hash-mismatch", f"canonical sha mismatch vs {source}")


def _recoverable(
    reference: MsTriple, hypothesis: Sequence[MsTriple], normalize: TextNormalizer,
) -> bool:
    """True iff some run of >=2 adjacent hypothesis cues concatenates
    (normalized) to the reference text AND its span overlaps the reference
    span (half-open ``[start, end)`` on both sides)."""
    target = normalize(reference[2])
    if not target:
        return False
    for index, (start_i, _end_i, text_i) in enumerate(hypothesis):
        concatenated = normalize(text_i)
        for j in range(index + 1, len(hypothesis)):
            concatenated += normalize(hypothesis[j][2])
            if len(concatenated) > len(target):
                break
            if (concatenated == target
                    and start_i < reference[1] and reference[0] < hypothesis[j][1]):
                return True
    return False


def diagnose_segmentation(
    hypothesis: Sequence[MsTriple],
    reference: Sequence[MsTriple],
    normalize: TextNormalizer,
) -> dict[str, int]:
    """Counts-only deterministic segmentation diagnosis (plan Task 2).

    Omitted reference utterance (strip-exact absence — the production
    ``omitted_utterance_count`` criterion): ``segmentation_merge_split`` when
    fully recoverable from adjacent hypothesis-cue concatenation with
    temporal overlap, else ``actual_missing``. Duplicated hypothesis cue
    (occurrences of a strip-exact text beyond the reference count — the
    production ``duplicated_utterance_count`` criterion): the occurrence
    order is deterministic (the k-th occurrence for k <= reference count is
    expected; later ones are extras); an extra is ``segmentation_split``
    when its normalized non-empty text is a substring of one reference
    utterance's normalized text, else ``actual_duplication``.
    """
    hypothesis_texts = [text.strip() for _, _, text in hypothesis]
    omitted = [ref for ref in reference if ref[2].strip() not in hypothesis_texts]
    merge_split = sum(
        1 for ref in omitted if _recoverable(ref, hypothesis, normalize))
    expected_counts = Counter(text.strip() for _, _, text in reference)
    normalized_refs = [normalize(text) for _, _, text in reference]
    seen: Counter[str] = Counter()
    segmentation_split = 0
    actual_duplication = 0
    for _, _, text in hypothesis:
        key = text.strip()
        seen[key] += 1
        if seen[key] <= expected_counts.get(key, 0):
            continue
        normalized = normalize(key)
        if normalized and any(normalized in ref for ref in normalized_refs):
            segmentation_split += 1
        else:
            actual_duplication += 1
    return {
        "omitted_total": len(omitted),
        "segmentation_merge_split": merge_split,
        "actual_missing": len(omitted) - merge_split,
        "duplicated_total": segmentation_split + actual_duplication,
        "segmentation_split": segmentation_split,
        "actual_duplication": actual_duplication,
    }


def diagnosis_lines(diagnosis: dict[str, int]) -> list[str]:
    """Marked counts-only block for the diagnosis evidence file."""
    return [
        "diagnosis-table-begin",
        (f"omitted_total={diagnosis['omitted_total']} "
         f"segmentation_merge_split={diagnosis['segmentation_merge_split']} "
         f"actual_missing={diagnosis['actual_missing']}"),
        (f"duplicated_total={diagnosis['duplicated_total']} "
         f"segmentation_split={diagnosis['segmentation_split']} "
         f"actual_duplication={diagnosis['actual_duplication']}"),
        "diagnosis-table-end",
    ]


def quality_string_allowed(value: str) -> bool:
    """Task 1's committed-output allowlist extended with quality labels."""
    return value in QUALITY_LABELS or string_allowed(value)


def assert_quality_sanitized(value: object, path: str = "$") -> None:
    """Recursively refuse any string outside the allowlist (values AND keys)."""
    if isinstance(value, str):
        if not quality_string_allowed(value):
            raise QualityRefusalError(
                "unsanitized-output", f"string at {path} len={len(value)}")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not (
                FIELD_KEY_RE.fullmatch(key) or quality_string_allowed(key)
            ):
                raise QualityRefusalError("unsanitized-output", f"key at {path}")
            assert_quality_sanitized(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_quality_sanitized(item, f"{path}[{index}]")
    elif value is not None and not isinstance(value, bool | int | float):
        raise QualityRefusalError("unsanitized-output", f"type at {path}")


__all__ = [
    "QUALITY_LABELS", "REFUSAL_LABELS", "MetricValues", "QualityRefusalError",
    "TextNormalizer", "ThresholdLimits", "assert_quality_sanitized", "convert_cues",
    "diagnose_segmentation", "diagnosis_lines", "gate_checks", "quality_string_allowed",
    "relative_ms", "stability_check", "verify_hash"]
