"""Recall-audit sentinel (PRD 7.7, implementation plan 6.7).

Stratified deterministic sampling from the LOW-score stratum of task 16's
``ProgressivePlan`` triage rows, plus miss-rate evaluation that can flag
the coarse triage policy as ``degraded`` — a state the Media Intelligence
Gate (task 19) is allowed to fail on even when high-score candidates look
good.  A run with no sample (or no reviews) is a typed error, never a
pass: without a sentinel that actually ran, a low Missed Valuable Moment
Rate cannot be trusted.

Deep reviews are consumed as DATA only — this module never executes a
review, an LLM call, or any timeline mutation.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BeforeValidator, Field

from services.contracts.primitives import Identifier, StrictModel, to_tuple

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.media_intelligence.progressive import TriageEntry


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class RecallAuditError(ValueError):
    """Base recall-audit failure."""

    LABEL = "recall_audit_error"


class RecallAuditNotRunError(RecallAuditError):
    """Sentinel未実施 — a run with no sample (or no reviews) is an error."""

    LABEL = "recall_audit_not_run"


class RecallAuditReviewMismatchError(RecallAuditError):
    """A review targets a shot outside the drawn sample, or duplicates one."""

    LABEL = "recall_audit_review_mismatch"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class RecallAuditPolicy(StrictModel):
    """Stratification knobs and the degradation threshold."""

    sample_per_stratum: int = Field(default=3, ge=1)
    stratum_count: int = Field(default=4, ge=2, le=10)
    low_score_threshold: float | None = Field(default=None, ge=0, le=1)
    degraded_miss_rate_threshold: float = Field(default=0.2, ge=0, le=1)


class SampledShot(StrictModel):
    """One sampled low-stratum shot with its stratum label."""

    shot_id: Identifier
    router_score: float = Field(ge=0, le=1)
    stratum: str


class RecallAuditSample(StrictModel):
    """Drawn sentinel sample (``recall-audit-sample-v1``)."""

    schema_version: Literal["recall-audit-sample-v1"] = "recall-audit-sample-v1"
    sampled: Annotated[tuple[SampledShot, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )


class AuditReviewResult(StrictModel):
    """Structured deep-review outcome for one sampled shot (data only).

    Adapter for task 17's ``MomentDeepReviewV1``: once its assessment
    shape lands, map a non-empty ``keep_rationale_candidates`` on a
    LOW-stratum shot to ``missed_value=True`` via
    :meth:`from_moment_review`.  The adapter reads the field duck-typed
    and fails loudly with ``TypeError`` when the shape differs, rather
    than silently mis-mapping.
    """

    shot_id: Identifier
    missed_value: bool

    @classmethod
    def from_moment_review(cls, shot_id: str, review: object) -> AuditReviewResult:
        candidates = getattr(review, "keep_rationale_candidates", None)
        if candidates is None:
            raise TypeError(
                f"review for {shot_id!r} has no keep_rationale_candidates field"
            )
        return cls(shot_id=shot_id, missed_value=bool(candidates))


class RecallAuditReportV1(StrictModel):
    """Sentinel report (``recall-audit-v1``) — the Gate may fail on it."""

    schema_version: Literal["recall-audit-v1"] = "recall-audit-v1"
    sampled_count: int = Field(ge=0)
    reviewed_count: int = Field(ge=0)
    valuable_miss_count: int = Field(ge=0)
    miss_rate: float = Field(ge=0, le=1)
    degradation_signal: Literal["ok", "degraded"]
    evidence_refs: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def _quantile_strata(
    rows: tuple[TriageEntry, ...], stratum_count: int
) -> tuple[tuple[str, tuple[TriageEntry, ...]], ...]:
    """Score-value quantile buckets, ascending (task-16 semantics).

    Bucket boundaries are SCORE values at rank ``ceil(n*(j+1)/K)-1`` so
    equal scores always stay in the same bucket; the last bucket holds
    everything strictly above the final boundary.
    """

    count = len(rows)
    boundaries = tuple(
        rows[min(count - 1, math.ceil(count * (index + 1) / stratum_count) - 1)].router_score
        for index in range(stratum_count - 1)
    )
    strata: list[tuple[str, tuple[TriageEntry, ...]]] = []
    lower: float | None = None
    for index, upper in enumerate(boundaries):
        members = tuple(
            row
            for row in rows
            if (lower is None or row.router_score > lower) and row.router_score <= upper
        )
        strata.append((f"q{index + 1}", members))
        lower = upper
    strata.append(
        (
            f"q{stratum_count}",
            tuple(row for row in rows if row.router_score > boundaries[-1]),
        )
    )
    return tuple(strata)


def draw_audit_sample(
    triage_table: Sequence[TriageEntry], *, policy: RecallAuditPolicy
) -> RecallAuditSample:
    """Deterministically sample the low strata of a triage table.

    Strata are score-quantile buckets over the whole table (default
    quartiles) minus the top bucket; or, when ``low_score_threshold`` is
    set, a single explicit ``low`` stratum of shots strictly below the
    threshold.  Within each stratum rows are sorted by
    ``(router_score, shot_id)`` and the first ``sample_per_stratum`` are
    taken — seedless, no randomness.

    Raises :class:`RecallAuditNotRunError` when nothing can be sampled.
    """

    rows = tuple(sorted(triage_table, key=lambda row: (row.router_score, row.shot_id)))
    if policy.low_score_threshold is not None:
        strata: tuple[tuple[str, tuple[TriageEntry, ...]], ...] = (
            (
                "low",
                tuple(
                    row for row in rows if row.router_score < policy.low_score_threshold
                ),
            ),
        )
    elif rows:
        strata = _quantile_strata(rows, policy.stratum_count)[:-1]
    else:
        strata = ()

    sampled = [
        SampledShot(shot_id=row.shot_id, router_score=row.router_score, stratum=label)
        for label, members in strata
        for row in members[: policy.sample_per_stratum]
    ]
    if not sampled:
        raise RecallAuditNotRunError(
            "recall-audit sentinel drew an empty sample: no low-stratum shots to audit"
        )
    return RecallAuditSample(
        sampled=tuple(
            sorted(sampled, key=lambda shot: (shot.stratum, str(shot.shot_id)))
        )
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_recall(
    audit_reviews: Sequence[AuditReviewResult],
    *,
    sample: RecallAuditSample,
    policy: RecallAuditPolicy,
) -> RecallAuditReportV1:
    """Evaluate sentinel reviews into a miss-rate report.

    ``miss_rate`` is exact rational math (``Fraction``) — the signal is
    ``degraded`` when the miss rate is greater than or equal to
    ``degraded_miss_rate_threshold`` (default 0.2).

    Raises :class:`RecallAuditNotRunError` when the sample is empty or no
    review was returned, and :class:`RecallAuditReviewMismatchError` when
    a review targets a shot outside the drawn sample or duplicates one.
    """

    sampled_ids = {str(shot.shot_id) for shot in sample.sampled}
    if not sampled_ids:
        raise RecallAuditNotRunError("recall-audit sentinel has no sample: not run")

    reviews = tuple(audit_reviews)
    seen: set[str] = set()
    for review in reviews:
        shot_id = str(review.shot_id)
        if shot_id not in sampled_ids:
            raise RecallAuditReviewMismatchError(
                f"review targets a shot outside the audit sample: {shot_id}"
            )
        if shot_id in seen:
            raise RecallAuditReviewMismatchError(f"duplicate review for shot: {shot_id}")
        seen.add(shot_id)
    if not reviews:
        raise RecallAuditNotRunError("recall-audit sentinel reviewed no sample shots")

    misses = tuple(sorted(str(review.shot_id) for review in reviews if review.missed_value))
    miss_fraction = Fraction(len(misses), len(reviews))
    threshold = Fraction(str(policy.degraded_miss_rate_threshold))
    signal: Literal["ok", "degraded"] = "degraded" if miss_fraction >= threshold else "ok"
    return RecallAuditReportV1(
        sampled_count=len(sampled_ids),
        reviewed_count=len(reviews),
        valuable_miss_count=len(misses),
        miss_rate=float(miss_fraction),
        degradation_signal=signal,
        evidence_refs=tuple(f"miss:{shot_id}" for shot_id in misses),
    )
