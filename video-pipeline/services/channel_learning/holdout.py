"""Holdout evaluation — profile version bump + before/after comparison.

Separation contract: This module defines holdout evaluation artifacts
and the approval/bump/evaluation flow for governed profile change
proposals. It keeps audience-outcome ingestion separate from preference
holdout logic by import boundary — no symbol from the observation
artifact nor from the reference taste artifact may be imported or
re-exported here. Holdout compares generic numeric metrics
(before/after) without coupling to the PerformanceObservation schema;
audience outcomes stay separate.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.channel_learning.proposal import ChannelProfileChangeProposalV1  # noqa: TC001
from services.contracts.primitives import Identifier, StrictModel

_DELTA_TOLERANCE: float = 1e-9

# ---------------------------------------------------------------------------
# Profile version bump record
# ---------------------------------------------------------------------------


class ProfileVersionBumpV1(StrictModel):
    """Record of a profile version bump triggered by an approved proposal."""

    schema_version: Literal["profile-version-bump-v1"] = "profile-version-bump-v1"
    bump_id: Identifier
    proposal_id: Identifier
    from_version: Annotated[int, Field(ge=0, strict=True)]
    to_version: Annotated[int, Field(ge=0, strict=True)]

    @model_validator(mode="after")
    def require_increment_by_one(self) -> ProfileVersionBumpV1:
        if self.to_version != self.from_version + 1:
            raise PydanticCustomError(
                "version_not_incremented",
                "to_version must equal from_version + 1",
            )
        return self


# ---------------------------------------------------------------------------
# Holdout evaluation artifact
# ---------------------------------------------------------------------------


class HoldoutEvaluationV1(StrictModel):
    """Holdout evaluation comparing before/after after a profile change.

    Records the outcome delta on later episodes after the proposal was
    applied at a specific profile version. The evaluation does not
    depend on PerformanceObservation; it carries plain numeric metrics
    so the preference/holdout layer stays separate from audience-outcome
    ingestion.
    """

    schema_version: Literal["holdout-evaluation-v1"] = "holdout-evaluation-v1"
    evaluation_id: Identifier
    proposal_id: Identifier
    profile_version_before: Annotated[int, Field(ge=0, strict=True)]
    profile_version_after: Annotated[int, Field(ge=0, strict=True)]
    before_metric: float
    after_metric: float
    delta: float

    @model_validator(mode="after")
    def require_delta_consistency(self) -> HoldoutEvaluationV1:
        expected = self.after_metric - self.before_metric
        if abs(self.delta - expected) > _DELTA_TOLERANCE:
            raise PydanticCustomError(
                "delta_mismatch",
                "delta must equal after_metric - before_metric",
            )
        if self.profile_version_after != self.profile_version_before + 1:
            raise PydanticCustomError(
                "holdout_version_not_incremented",
                "profile_version_after must equal profile_version_before + 1",
            )
        return self


# ---------------------------------------------------------------------------
# Status transition helpers — no auto-application
# ---------------------------------------------------------------------------


def approve_proposal(
    proposal: ChannelProfileChangeProposalV1,
) -> ChannelProfileChangeProposalV1:
    """Approve a draft proposal. Only draft -> approved is allowed."""

    if proposal.status != "draft":
        raise PydanticCustomError(
            "invalid_status_transition",
            "only draft proposals can be approved",
        )
    return proposal.model_copy(update={"status": "approved"})


def reject_proposal(
    proposal: ChannelProfileChangeProposalV1,
) -> ChannelProfileChangeProposalV1:
    """Reject a draft proposal. Only draft -> rejected is allowed."""

    if proposal.status != "draft":
        raise PydanticCustomError(
            "invalid_status_transition",
            "only draft proposals can be rejected",
        )
    return proposal.model_copy(update={"status": "rejected"})


def apply_proposal(
    proposal: ChannelProfileChangeProposalV1,
    *,
    current_version: int,
    bump_id: str = "bump-0001",
) -> tuple[ChannelProfileChangeProposalV1, ProfileVersionBumpV1]:
    """Apply an approved proposal and emit a version bump record.

    Human approval is the only path to applied; draft or rejected
    proposals cannot be applied (ValueError via PydanticCustomError).
    """

    if proposal.status != "approved":
        raise PydanticCustomError(
            "invalid_status_transition",
            "only approved proposals can be applied",
        )
    if current_version < 0:
        raise PydanticCustomError(
            "invalid_version",
            "current_version must be >= 0",
        )
    bump = ProfileVersionBumpV1(
        bump_id=bump_id,  # type: ignore[arg-type]
        proposal_id=proposal.proposal_id,
        from_version=current_version,
        to_version=current_version + 1,
    )
    applied = proposal.model_copy(update={"status": "applied"})
    return applied, bump


def evaluate_holdout(
    proposal: ChannelProfileChangeProposalV1,
    bump: ProfileVersionBumpV1,
    *,
    before_metric: float,
    after_metric: float,
    evaluation_id: str = "eval-0001",
) -> HoldoutEvaluationV1:
    """Evaluate holdout on later episodes after an applied proposal.

    Requires proposal.status == "applied" and bump.proposal_id to match.
    before_metric / after_metric are plain floats (e.g., CTR, retention)
    kept separate from PerformanceObservation to preserve the separation
    contract. Human approval + bump is the only path; draft/approved
    proposals are rejected.
    """

    if proposal.status != "applied":
        raise PydanticCustomError(
            "invalid_status_transition",
            "only applied proposals can be holdout-evaluated",
        )
    if bump.proposal_id != proposal.proposal_id:
        raise PydanticCustomError(
            "bump_proposal_mismatch",
            "bump.proposal_id must match proposal.proposal_id",
        )

    delta = after_metric - before_metric
    return HoldoutEvaluationV1(
        evaluation_id=evaluation_id,  # type: ignore[arg-type]
        proposal_id=proposal.proposal_id,
        profile_version_before=bump.from_version,
        profile_version_after=bump.to_version,
        before_metric=before_metric,
        after_metric=after_metric,
        delta=delta,
    )
