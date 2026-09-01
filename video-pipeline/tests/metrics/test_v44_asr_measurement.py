"""ASR measurement policy v2 (predeclared 2026-09-01) — unit contracts.

Red-first tests for the alignment-based measurement that replaces the
exact-text omitted/duplicated counts and the untimed greedy pairing
(plan ``.omo/plans/v44-asr-measurement-policy-v2.md``; PRD §6.7.4
predeclaration). Every fixture is synthetic Japanese; thresholds are NOT
under test here and are never asserted.
"""

from __future__ import annotations

from typing import Final

from services.metrics.v44_asr_measurement import (
    PAIR_CER_MAX,
    POLICY_VERSION,
    align_utterances,
    measure_asr_alignment_v2,
    to_evidence_quality,
)
from services.metrics.v44_product_proof import (
    TranscriptSampleV1,
    TranscriptSegment,
)

EXPECTED_POLICY_VERSION: Final = "v44-asr-measurement-v2"


def seg(start_ms: int, end_ms: int, text: str) -> TranscriptSegment:
    return TranscriptSegment(start_ms=start_ms, end_ms=end_ms, text=text)


def sample(*segments: TranscriptSegment) -> TranscriptSampleV1:
    return TranscriptSampleV1(segments=segments)


def test_policy_version_and_pair_threshold_are_declared() -> None:
    """Given: the predeclared policy; Then: its version id and the pairing
    ceiling are the frozen constants the evidence note quotes."""
    assert POLICY_VERSION == EXPECTED_POLICY_VERSION
    assert PAIR_CER_MAX == 0.5


def test_one_char_difference_never_counts_as_omitted_nor_duplicated() -> None:
    """Given: a hypothesis whose first utterance differs by one character
    (v1 counted this pair as omitted+1 AND duplicated+1); When: measured
    under v2; Then: both utterances pair, nothing is omitted or duplicated."""
    corrected = sample(
        seg(0, 1000, "今日は良い天気です。"),
        seg(1000, 2000, "次の話題です。"),
    )
    hypothesis = (
        seg(50, 1050, "今日は良い天気ですよ"),
        seg(1050, 2050, "次の話題です。"),
    )
    measurement = measure_asr_alignment_v2(corrected, hypothesis)
    assert measurement.paired_count == 2
    assert measurement.omitted_utterances == 0
    assert measurement.duplicated_utterances == 0
    assert measurement.segmentation_split_count == 0


def test_split_recovery_classifies_as_split_not_duplicated() -> None:
    """Given: one reference utterance split into two adjacent hypothesis
    fragments whose concatenation recovers it; When: measured; Then: the
    unaligned fragment is a segmentation split, never a duplication."""
    corrected = sample(seg(0, 2000, "今日はとても良い天気です"))
    hypothesis = (
        seg(0, 1000, "今日はとても"),
        seg(1000, 2000, "良い天気です"),
    )
    measurement = measure_asr_alignment_v2(corrected, hypothesis)
    assert measurement.omitted_utterances == 0
    assert measurement.duplicated_utterances == 0
    assert measurement.segmentation_split_count == 1


def test_true_repeat_counts_as_duplicated() -> None:
    """Given: an extra hypothesis utterance near-matching an already-paired
    reference at a distant time; When: measured; Then: it is a duplication
    (a repeat the subtitle track would render twice), not a split."""
    corrected = sample(
        seg(0, 1000, "そうですね"),
        seg(5000, 6000, "違う話をします"),
    )
    hypothesis = (
        seg(0, 1000, "そうですね"),
        seg(6000, 7000, "そうでね"),
        seg(7000, 8000, "違う話をします"),
    )
    measurement = measure_asr_alignment_v2(corrected, hypothesis)
    assert measurement.paired_count == 2
    assert measurement.duplicated_utterances == 1
    assert measurement.segmentation_split_count == 0


def test_align_prefers_local_monotonic_match_over_distant_identical_text() -> None:
    """Given: the r1 failure shape — a short utterance repeated seconds
    apart, which untimed text-only pairing could join across the gap;
    When: aligned; Then: order-preserving local pairs hold and every diff
    stays sub-second."""
    corrected = sample(
        seg(0, 1000, "うん"),
        seg(60000, 61000, "長い文のほうです"),
    )
    hypothesis = (
        seg(600, 1600, "うん"),
        seg(60100, 61100, "長い文のほうです"),
    )
    measurement = measure_asr_alignment_v2(corrected, hypothesis)
    assert measurement.paired_count == 2
    assert measurement.timestamp_error_p95_ms is not None
    assert measurement.timestamp_error_p95_ms <= 600.0
    assert measurement.outlier_count_over_1000ms == 0


def test_p95_includes_genuine_drift_pairs() -> None:
    """Given: an exact-text pair whose start drifted by 8 seconds (the real
    tail-drift class); When: measured; Then: the drift stays in the diffs —
    p95 carries it and the outlier counter sees it. v2 never hides it."""
    corrected = sample(
        seg(0, 1000, "最初の発話です"),
        seg(9000, 10000, "次の発話です"),
    )
    hypothesis = (
        seg(0, 1000, "最初の発話です"),
        seg(17000, 18000, "次の発話です"),
    )
    measurement = measure_asr_alignment_v2(corrected, hypothesis)
    assert measurement.paired_count == 2
    assert measurement.timestamp_error_p95_ms == 8000.0
    assert measurement.outlier_count_over_1000ms == 1


def test_align_never_pairs_below_similarity_threshold() -> None:
    """Given: texts further apart than the pairing ceiling; When: measured;
    Then: no pair exists — the reference is omitted, the hypothesis segment
    is neither split nor duplicated, and p95 is null (fail-closed)."""
    corrected = sample(seg(0, 1000, "全然違うはなし"))
    hypothesis = (seg(0, 1000, "まったく別の内容"),)
    measurement = measure_asr_alignment_v2(corrected, hypothesis)
    assert measurement.paired_count == 0
    assert measurement.omitted_utterances == 1
    assert measurement.duplicated_utterances == 0
    assert measurement.timestamp_error_p95_ms is None


def test_align_is_one_to_one_and_order_preserving() -> None:
    """Given: three references whose texts reappear in a crossed hypothesis
    order; When: aligned; Then: the alignment is one-to-one and monotonic —
    both index sequences strictly increase along the pairs (near-text
    cross-position pairs may legally exist; crossing pairs may not)."""
    corrected = sample(
        seg(0, 1000, "一番の話"),
        seg(1000, 2000, "二番の話"),
        seg(2000, 3000, "三番の話"),
    )
    hypothesis = (
        seg(5000, 6000, "三番の話"),
        seg(100, 1100, "一番の話"),
        seg(1100, 2100, "二番の話"),
    )
    pairs = align_utterances(corrected.segments, hypothesis)
    assert len(pairs) >= 2
    reference_indices = [pair.reference_index for pair in pairs]
    hypothesis_indices = [pair.hypothesis_index for pair in pairs]
    assert reference_indices == sorted(set(reference_indices))
    assert hypothesis_indices == sorted(set(hypothesis_indices))


def test_align_deterministic_tie_break_prefers_earliest_indices() -> None:
    """Given: one reference and two identical hypothesis utterances at
    different times (equal pairing cost); When: aligned twice; Then: the
    canonical earliest-index pair is chosen, identically on both runs."""
    corrected = sample(seg(1000, 1100, "あ"))
    hypothesis = (seg(1200, 1300, "あ"), seg(9000, 9100, "あ"))
    first = align_utterances(corrected.segments, hypothesis)
    second = align_utterances(corrected.segments, hypothesis)
    assert first == second
    assert len(first) == 1
    assert first[0].hypothesis_index == 0
    assert first[0].signed_diff_ms == 200


def test_counts_sum_invariants_hold_on_a_mixed_fixture() -> None:
    """Given: a fixture exercising all classes (paired, omitted, split,
    duplicated, insertion); When: measured; Then: the counting invariants
    hold: omitted + paired == references; split + duplicated <= unaligned."""
    corrected = sample(
        seg(0, 1000, "最初の発話です"),
        seg(1000, 2000, "二番目の発話"),
        seg(2000, 3000, "三番目の発話"),
        seg(3000, 4000, "まったく関係のない長い話題"),
    )
    hypothesis = (
        seg(50, 1050, "最初の発話です"),  # paired
        seg(1050, 1500, "二番目の"),  # split half A
        seg(1500, 2050, "発話"),  # split half B
        seg(2100, 3100, "三番目の発話です"),  # near-miss pair (1 char added)
        seg(60000, 61000, "最初の発話です"),  # true repeat -> duplicated
        seg(70000, 71000, "幻聴の完全な別文"),  # insertion: neither class
    )
    measurement = measure_asr_alignment_v2(corrected, hypothesis)
    assert measurement.policy_version == EXPECTED_POLICY_VERSION
    assert measurement.reference_count == 4
    assert measurement.hypothesis_count == 6
    assert measurement.omitted_utterances + measurement.paired_count == 4
    assert (
        measurement.segmentation_split_count + measurement.duplicated_utterances
        <= measurement.hypothesis_count - measurement.paired_count
    )
    assert measurement.omitted_utterances == 1
    assert measurement.duplicated_utterances == 1
    assert measurement.segmentation_split_count == 1


def test_measurement_maps_into_evidence_quality_fields() -> None:
    """Given: a v2 measurement; When: projected onto the existing
    EvidenceQualityMetrics; Then: the five gate fields carry the v2 values
    and the schema itself is untouched."""
    corrected = sample(seg(0, 1000, "同じ文です"))
    hypothesis = (seg(100, 1100, "同じ文です"),)
    measurement = measure_asr_alignment_v2(corrected, hypothesis)
    quality = to_evidence_quality(measurement, cer_value=0.05, pn_recall=1.0)
    assert quality.transcript_cer == 0.05
    assert quality.proper_noun_recall == 1.0
    assert quality.timestamp_error_p95_ms == measurement.timestamp_error_p95_ms
    assert quality.omitted_utterances == measurement.omitted_utterances
    assert quality.duplicated_utterances == measurement.duplicated_utterances
