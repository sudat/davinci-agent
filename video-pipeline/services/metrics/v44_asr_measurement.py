"""Predeclared ASR measurement policy v2 (2026-09-01).

One canonical alignment between a corrected transcript sample and an ASR
hypothesis, plus the counting semantics derived from it:

- monotonic (order-preserving) one-to-one pairing over normalized-CER <= 0.5
  candidate edges, maximum cardinality first, then minimum total cost
  (exact :class:`fractions.Fraction` arithmetic — no float tie hazards);
  equal-cost cells resolve along the fixed evaluation order (skip reference,
  skip hypothesis, pair), which is the canonical earliest-index tie-break;
- ``omitted``    — unpaired reference utterances (v1 counted a one-character
  difference as an omission AND its variant as a duplication);
- ``duplicated`` — unaligned hypothesis utterances that near-match an
  already-paired reference (true repeats the subtitle track would render
  twice);
- ``segmentation_split_count`` — unaligned hypothesis utterances recoverable
  by concatenating >=2 adjacent hypothesis utterances (containing the
  segment) into one reference with temporal overlap — the same discipline
  as the closed resolve-auto-caption spike's ``quality_logic._recoverable``;
  reported for diagnosis, never counted as duplication;
- ``timestamp_error_p95_ms`` — nearest-rank p95 over ALL paired start-time
  diffs; genuine drift pairs are never excluded.

Predeclaration: ``.omo/plans/v44-asr-measurement-policy-v2.md`` and PRD
v4.4 §6.7.4. The four numeric thresholds are NOT defined here and are NOT
changed by this policy — they stay frozen in
:mod:`services.cli._v44_arm_transcript`.

The v1 functions in :mod:`services.metrics.v44_product_proof` and
:mod:`services.cli._v44_jp_metrics` remain byte-identical so every closed
report keeps the interpretation it was written under.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from fractions import Fraction
from typing import Final

from pydantic import Field

from services.contracts.primitives import StrictModel
from services.metrics.v44_product_proof import (
    EvidenceQualityMetrics,
    TranscriptSampleV1,
    TranscriptSegment,
    _levenshtein,
    cer,
    timestamp_error_p95_ms,
)

POLICY_VERSION: Final = "v44-asr-measurement-v2"
PAIR_CER_MAX: Final = 0.5
#: |start diff| strictly over this bound counts as an outlier (report-only).
OUTLIER_MS: Final = 1000.0

#: Same strip set as ``services.cli._v44_jp_metrics._STRIP_RE`` (which
#: mirrors ``services.toolchain.whisper_ja._normalize_japanese`` plus the
#: ellipsis/middle-dot characters): punctuation, brackets, and whitespace
#: never count as transcript content.
_STRIP_RE = re.compile(r"[、。.,!?\s‥…「」『』・\u3000]")

#: DP cell: (negative pairing cardinality, exact total cost) — the
#: lexicographic minimum wins.
type _Cell = tuple[int, Fraction]


def _normalize(text: str) -> str:
    return _STRIP_RE.sub("", text)


def _edge_cost(reference: str, hypothesis: str) -> Fraction | None:
    """Exact CER edge (distance / reference length) when pairable, else None."""
    if not reference or not hypothesis:
        return None
    cost = Fraction(_levenshtein(reference, hypothesis), len(reference))
    return cost if cost <= PAIR_CER_MAX else None


class AlignmentPair(StrictModel):
    """One monotonic reference-hypothesis pair under policy v2."""

    reference_index: int = Field(ge=0, strict=True)
    hypothesis_index: int = Field(ge=0, strict=True)
    reference_start_ms: int = Field(strict=True)
    hypothesis_start_ms: int = Field(strict=True)
    cer: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    signed_diff_ms: int = Field(strict=True)


class AsrAlignmentMeasurementV2(StrictModel):
    """Runtime measurement value (NOT an authoritative artifact family)."""

    policy_version: str
    reference_count: int = Field(ge=0, strict=True)
    hypothesis_count: int = Field(ge=0, strict=True)
    paired_count: int = Field(ge=0, strict=True)
    omitted_utterances: int = Field(ge=0, strict=True)
    duplicated_utterances: int = Field(ge=0, strict=True)
    segmentation_split_count: int = Field(ge=0, strict=True)
    signed_median_ms: int | None = None
    abs_median_ms: int | None = None
    outlier_count_over_1000ms: int = Field(ge=0, strict=True)
    timestamp_error_p95_ms: float | None = Field(default=None, allow_inf_nan=False)
    diffs_ms: tuple[int, ...] = Field(default_factory=tuple)


def align_utterances(
    reference: Sequence[TranscriptSegment], hypothesis: Sequence[TranscriptSegment]
) -> tuple[AlignmentPair, ...]:
    """Maximum-cardinality monotonic one-to-one alignment (see module docstring)."""
    refs, hyps = tuple(reference), tuple(hypothesis)
    norm_r = [_normalize(seg.text) for seg in refs]
    norm_h = [_normalize(seg.text) for seg in hyps]
    n, m = len(refs), len(hyps)
    costs = [[_edge_cost(norm_r[i], norm_h[j]) for j in range(m)] for i in range(n)]

    zero = (0, Fraction(0))
    table: list[list[_Cell]] = [[zero] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = table[i - 1][j]  # skip reference — canonical tie priority 1
            skip_hyp = table[i][j - 1]  # skip hypothesis — priority 2
            best = min(best, skip_hyp)
            edge = costs[i - 1][j - 1]  # pair — priority 3
            if edge is not None:
                prev = table[i - 1][j - 1]
                paired = (prev[0] - 1, prev[1] + edge)
                best = min(best, paired)
            table[i][j] = best

    pairs: list[AlignmentPair] = []
    i, j = n, m
    while i > 0 and j > 0:
        cell = table[i][j]
        if cell == table[i - 1][j]:  # mirror of the forward tie priority
            i -= 1
        elif cell == table[i][j - 1]:
            j -= 1
        else:
            edge = costs[i - 1][j - 1]
            prev = table[i - 1][j - 1]
            if edge is None or cell != (prev[0] - 1, prev[1] + edge):
                raise RuntimeError("alignment backtrack invariant violated")
            pairs.append(
                AlignmentPair(
                    reference_index=i - 1,
                    hypothesis_index=j - 1,
                    reference_start_ms=refs[i - 1].start_ms,
                    hypothesis_start_ms=hyps[j - 1].start_ms,
                    cer=cer(norm_r[i - 1], norm_h[j - 1]),
                    signed_diff_ms=hyps[j - 1].start_ms - refs[i - 1].start_ms,
                )
            )
            i, j = i - 1, j - 1
    pairs.reverse()
    return tuple(pairs)


def _is_split(
    h_index: int,
    norm_h: Sequence[str],
    hyps: Sequence[TranscriptSegment],
    refs: Sequence[TranscriptSegment],
    norm_r: Sequence[str],
) -> bool:
    """Segmentation-split recovery: some run of >=2 adjacent hypothesis
    utterances containing ``h_index`` concatenates (normalized) to exactly
    one reference utterance whose half-open span the run overlaps."""
    target = {text: index for index, text in enumerate(norm_r) if text}
    if not target:
        return False
    max_len = max(len(text) for text in target)
    for start in range(h_index + 1):
        concatenation = norm_h[start]
        for last in range(start + 1, len(norm_h)):
            concatenation += norm_h[last]
            if len(concatenation) > max_len:
                break
            if not start <= h_index <= last:
                continue
            hit = target.get(concatenation)
            if hit is None:
                continue
            if (
                hyps[start].start_ms < refs[hit].end_ms
                and refs[hit].start_ms < hyps[last].end_ms
            ):
                return True
    return False


def _nearest_rank_median(values: Sequence[int]) -> int | None:
    if not values:
        return None
    rank = (len(values) + 1) // 2  # nearest-rank median, 1-indexed
    return values[rank - 1]


def measure_asr_alignment_v2(
    corrected: TranscriptSampleV1 | Sequence[TranscriptSegment],
    hypothesis_segments: Sequence[TranscriptSegment],
) -> AsrAlignmentMeasurementV2:
    """Full v2 measurement of one hypothesis against the corrected sample."""
    refs = corrected.segments if isinstance(corrected, TranscriptSampleV1) else tuple(corrected)
    hyps = tuple(hypothesis_segments)
    pairs = align_utterances(refs, hyps)
    paired_hyp = {pair.hypothesis_index for pair in pairs}
    aligned_ref = {pair.reference_index for pair in pairs}
    norm_h = [_normalize(seg.text) for seg in hyps]
    norm_r = [_normalize(seg.text) for seg in refs]

    split = 0
    duplicated = 0
    for j, normalized in enumerate(norm_h):
        if j in paired_hyp or not normalized:
            continue
        if _is_split(j, norm_h, hyps, refs, norm_r):
            split += 1
            continue
        if any(_edge_cost(norm_r[i], normalized) is not None for i in aligned_ref):
            duplicated += 1

    diffs = tuple(pair.signed_diff_ms for pair in pairs)
    return AsrAlignmentMeasurementV2(
        policy_version=POLICY_VERSION,
        reference_count=len(refs),
        hypothesis_count=len(hyps),
        paired_count=len(pairs),
        omitted_utterances=len(refs) - len(pairs),
        duplicated_utterances=duplicated,
        segmentation_split_count=split,
        signed_median_ms=_nearest_rank_median(sorted(diffs)),
        abs_median_ms=_nearest_rank_median(sorted(abs(diff) for diff in diffs)),
        outlier_count_over_1000ms=sum(1 for diff in diffs if abs(diff) > OUTLIER_MS),
        timestamp_error_p95_ms=timestamp_error_p95_ms([float(d) for d in diffs]),
        diffs_ms=diffs,
    )


def to_evidence_quality(
    measurement: AsrAlignmentMeasurementV2,
    *,
    cer_value: float,
    pn_recall: float | None = None,
) -> EvidenceQualityMetrics:
    """Project a v2 measurement onto the existing five-field gate model."""
    return EvidenceQualityMetrics(
        transcript_cer=cer_value,
        proper_noun_recall=pn_recall,
        timestamp_error_p95_ms=measurement.timestamp_error_p95_ms,
        omitted_utterances=measurement.omitted_utterances,
        duplicated_utterances=measurement.duplicated_utterances,
    )


__all__ = [
    "OUTLIER_MS",
    "PAIR_CER_MAX",
    "POLICY_VERSION",
    "AlignmentPair",
    "AsrAlignmentMeasurementV2",
    "align_utterances",
    "measure_asr_alignment_v2",
    "to_evidence_quality",
]
