"""Recall-audit sentinel (task 18).

Covers: deterministic stratified sampling from the low strata (score
quantile buckets and explicit low-threshold mode), miss-rate math on
synthetic reviews, the degradation-signal threshold boundary (0.19 -> ok,
0.20 -> degraded, configurable), the zero-sample typed not-run error, the
task-17 adapter shape for review results, review/sample mismatch
rejection, and report round-trip.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.foundation_io import canonical_model_bytes
from services.media_intelligence.progressive import TriageEntry
from services.media_intelligence.recall_audit import (
    AuditReviewResult,
    RecallAuditNotRunError,
    RecallAuditPolicy,
    RecallAuditReportV1,
    RecallAuditReviewMismatchError,
    RecallAuditSample,
    SampledShot,
    draw_audit_sample,
    evaluate_recall,
)


def _entry(shot_id: str, score: float) -> TriageEntry:
    return TriageEntry(
        shot_id=shot_id,
        role="coverage",
        story_relevance=score,
        select_potential=score,
        novelty=score,
        uncertainty=score,
        router_score=score,
    )


def _table(count: int) -> tuple[TriageEntry, ...]:
    """``count`` shots with distinct scores 0.05..0.05*count, ids in rank order."""

    return tuple(
        _entry(f"shot-{index + 1:02d}", round(0.05 * (index + 1), 6))
        for index in range(count)
    )


def _sample(count: int) -> RecallAuditSample:
    return RecallAuditSample(
        sampled=tuple(
            SampledShot(shot_id=f"shot-{index + 1:03d}", router_score=0.1, stratum="q1")
            for index in range(count)
        )
    )


def _reviews(count: int, miss_count: int) -> tuple[AuditReviewResult, ...]:
    return tuple(
        AuditReviewResult(shot_id=f"shot-{index + 1:03d}", missed_value=index < miss_count)
        for index in range(count)
    )


def test_same_triage_table_yields_identical_sample_bytes() -> None:
    table = _table(12)
    policy = RecallAuditPolicy(sample_per_stratum=2)

    first = draw_audit_sample(table, policy=policy)
    second = draw_audit_sample(table, policy=policy)
    shuffled = draw_audit_sample(tuple(reversed(table)), policy=policy)

    assert canonical_model_bytes(first) == canonical_model_bytes(second)
    assert canonical_model_bytes(first) == canonical_model_bytes(shuffled)


def test_sample_drawn_only_from_low_strata_no_top_quartile() -> None:
    table = _table(12)  # scores 0.05..0.60; top quartile = 0.50/0.55/0.60
    policy = RecallAuditPolicy(sample_per_stratum=2)

    sample = draw_audit_sample(table, policy=policy)

    for shot in sample.sampled:
        assert shot.router_score <= 0.45
        assert shot.router_score not in {0.50, 0.55, 0.60}
    assert {shot.stratum for shot in sample.sampled} == {"q1", "q2", "q3"}
    assert {shot.shot_id for shot in sample.sampled} == {
        "shot-01", "shot-02", "shot-04", "shot-05", "shot-07", "shot-08",
    }


def test_explicit_low_threshold_single_stratum_mode() -> None:
    table = _table(12)
    policy = RecallAuditPolicy(sample_per_stratum=3, low_score_threshold=0.2)

    sample = draw_audit_sample(table, policy=policy)

    assert {shot.shot_id for shot in sample.sampled} == {
        "shot-01", "shot-02", "shot-03",
    }
    assert all(shot.stratum == "low" for shot in sample.sampled)
    for shot in sample.sampled:
        assert shot.router_score < 0.2


def test_miss_rate_math_on_synthetic_reviews() -> None:
    sample = _sample(5)
    reviews = _reviews(5, miss_count=2)

    report = evaluate_recall(reviews, sample=sample, policy=RecallAuditPolicy())

    assert report.schema_version == "recall-audit-v1"
    assert report.sampled_count == 5
    assert report.reviewed_count == 5
    assert report.valuable_miss_count == 2
    assert report.miss_rate == pytest.approx(0.4)
    assert report.degradation_signal == "degraded"
    assert report.evidence_refs == ("miss:shot-001", "miss:shot-002")


@pytest.mark.parametrize(
    ("miss_count", "expected_signal", "expected_rate"),
    [(19, "ok", 0.19), (20, "degraded", 0.2)],
)
def test_degradation_signal_threshold_boundary(
    miss_count: int, expected_signal: str, expected_rate: float
) -> None:
    sample = _sample(100)
    reviews = _reviews(100, miss_count=miss_count)

    report = evaluate_recall(reviews, sample=sample, policy=RecallAuditPolicy())

    assert report.degradation_signal == expected_signal
    assert report.miss_rate == pytest.approx(expected_rate)


def test_degradation_threshold_is_configurable() -> None:
    sample = _sample(100)
    policy = RecallAuditPolicy(degraded_miss_rate_threshold=0.5)

    ok_report = evaluate_recall(_reviews(100, miss_count=30), sample=sample, policy=policy)
    degraded = evaluate_recall(_reviews(100, miss_count=50), sample=sample, policy=policy)

    assert ok_report.degradation_signal == "ok"
    assert degraded.degradation_signal == "degraded"


def test_zero_sample_is_typed_not_run_error() -> None:
    with pytest.raises(RecallAuditNotRunError, match="empty sample"):
        draw_audit_sample((), policy=RecallAuditPolicy())

    with pytest.raises(RecallAuditNotRunError, match="no sample"):
        evaluate_recall(
            _reviews(3, miss_count=1), sample=RecallAuditSample(), policy=RecallAuditPolicy()
        )

    with pytest.raises(RecallAuditNotRunError, match="reviewed no sample"):
        evaluate_recall((), sample=_sample(3), policy=RecallAuditPolicy())


def test_review_outside_sample_is_typed_mismatch() -> None:
    sample = _sample(2)
    policy = RecallAuditPolicy()

    with pytest.raises(RecallAuditReviewMismatchError, match="outside the audit sample"):
        evaluate_recall(
            (AuditReviewResult(shot_id="shot-999", missed_value=True),),
            sample=sample,
            policy=policy,
        )

    with pytest.raises(RecallAuditReviewMismatchError, match="duplicate"):
        evaluate_recall(
            (
                AuditReviewResult(shot_id="shot-001", missed_value=False),
                AuditReviewResult(shot_id="shot-001", missed_value=True),
            ),
            sample=sample,
            policy=policy,
        )


def test_from_moment_review_adapter_maps_keep_rationale_candidates() -> None:
    flagged = AuditReviewResult.from_moment_review(
        "shot-001", SimpleNamespace(keep_rationale_candidates=["strong reaction beat"])
    )
    clean = AuditReviewResult.from_moment_review(
        "shot-002", SimpleNamespace(keep_rationale_candidates=[])
    )

    assert flagged.missed_value is True
    assert clean.missed_value is False

    with pytest.raises(TypeError, match="keep_rationale_candidates"):
        AuditReviewResult.from_moment_review("shot-003", SimpleNamespace(other=1))


def test_report_and_sample_round_trip() -> None:
    sample = draw_audit_sample(_table(12), policy=RecallAuditPolicy(sample_per_stratum=2))
    reviews = (
        AuditReviewResult(shot_id="shot-01", missed_value=True),
        AuditReviewResult(shot_id="shot-02", missed_value=False),
    )
    report = evaluate_recall(reviews, sample=sample, policy=RecallAuditPolicy())
    again = evaluate_recall(reviews, sample=sample, policy=RecallAuditPolicy())

    assert RecallAuditSample.model_validate(sample.model_dump(mode="json")) == sample
    assert RecallAuditReportV1.model_validate(report.model_dump(mode="json")) == report
    assert canonical_model_bytes(report) == canonical_model_bytes(again)
