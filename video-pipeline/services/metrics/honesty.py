"""Honesty primitives: genuine approvals, KPI eligibility, claim support.

A "real" episode's human-time numbers are only as genuine as the
operator operation-record chain behind them: operator class, non-fixture,
editorial purpose, approve decision, not superseded (Todo 13/61 rules).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.metrics.report_models import (
    ClaimReview,
    DistributionMetric,
    EligibilitySection,
    FirstPassSection,
)

if TYPE_CHECKING:
    from services.metrics.bundle import EventBundle
    from services.metrics.models import AuthoredClaim, EpisodeDeclaration


def genuine_operator_approval(bundle: EventBundle, episode: EpisodeDeclaration) -> bool:
    """True only for a live (non-superseded) operator editorial approval."""

    record_id = episode.editorial_approval_record_id
    if record_id is None:
        return False
    superseded = {
        record.superseded_record_id
        for record in bundle.operation_records
        if record.superseded_record_id is not None
    }
    for record in bundle.operation_records:
        if record.record_id != record_id:
            continue
        return (
            record_id not in superseded
            and record.purpose == "editorial"
            and record.decision == "approve"
            and record.runner_class == "operator"
            and not record.fixture_only
        )
    return False


def kpi_eligible_ids(bundle: EventBundle) -> frozenset[str]:
    return frozenset(
        episode.episode_id
        for episode in bundle.episodes
        if episode.episode_kind == "real"
        and episode.in_contract
        and genuine_operator_approval(bundle, episode)
    )


def _distribution_claim(
    claim: AuthoredClaim, distribution: DistributionMetric, field: str
) -> ClaimReview:
    claimed_value = claim.value
    derived = getattr(distribution, field)
    if distribution.sample_count == 0 or derived is None:
        return ClaimReview(
            claim_id=claim.claim_id,
            supported=False,
            detail=(
                f"{claim.metric}: not_evaluable (sample_count=0; no eligible real "
                f"episodes) — claimed {claimed_value}"
            ),
        )
    return ClaimReview(
        claim_id=claim.claim_id,
        supported=derived == claimed_value,
        detail=f"{claim.metric}: recomputed {derived} vs claimed {claimed_value}",
    )


def _percent_claim(
    claim: AuthoredClaim, derived: int | None, label: str
) -> ClaimReview:
    if derived is None:
        return ClaimReview(
            claim_id=claim.claim_id,
            supported=False,
            detail=f"{claim.metric}: {label} is not_evaluable — claimed {claim.value}",
        )
    return ClaimReview(
        claim_id=claim.claim_id,
        supported=derived == claim.value,
        detail=f"{claim.metric}: recomputed {derived} vs claimed {claim.value}",
    )


def evaluate_claim(
    claim: AuthoredClaim,
    *,
    aht: DistributionMetric,
    first_pass: FirstPassSection,
    eligibility: EligibilitySection,
) -> ClaimReview:
    if claim.metric == "active_human_time_median_ms":
        return _distribution_claim(claim, aht, "median_ms")
    if claim.metric == "active_human_time_p90_ms":
        return _distribution_claim(claim, aht, "p90_ms")
    if claim.metric == "editorial_first_pass_rate_percent":
        return _percent_claim(claim, first_pass.rate_percent, "first-pass rate")
    return _percent_claim(
        claim, eligibility.coverage_ratio_percent, "coverage ratio"
    )


__all__ = [
    "evaluate_claim",
    "genuine_operator_approval",
    "kpi_eligible_ids",
]
