"""Strict Duration and Constraint Planner models (Todo 42).

These models carry the committed Selection Plan fields into the solver and
the solved/typed-infeasible outcomes out of it. Every duration is an exact
integer frame count — floats cannot be expressed anywhere — and the frozen
Phase-1 rule spec (``p1-budget-v1`` under ``p1-ordering-v1``) is bound
through the frozen manifest models, so an undeclared rule spec cannot reach
the solver. Report ``offending_candidate_ids`` tuples are derived from
canonically sorted structures so the same infeasible input yields
byte-identical reports under input permutation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Frame, Identifier, Sha256, StrictModel
from services.editorial.candidate_models import (  # noqa: TC001 (pydantic runtime fields)
    Candidate,
    CandidateSpan,
    SegmentKind,
)
from services.fixtures.manifest_phase1 import (  # noqa: TC001 (frozen rule spec at runtime)
    DurationBudgetRules,
    OrderingRules,
)
from services.validate.selection_models import EditSourceFacts  # noqa: TC001 (runtime field)

InfeasibilityKind = Literal[
    "must_include_conflict",
    "max_duration_exceeded",
    "lock_order_conflict",
    "capability_unavailable",
    "invalid_source",
]

PLANNER_INPUT_SCHEMA_VERSION: Literal["planner-input-v1"] = "planner-input-v1"


class ScoreWeights(StrictModel):
    """Declared soft-scoring weights (integers; the model proposes, never solves)."""

    content_weight: int = Field(ge=0, strict=True)
    clarity_weight: int = Field(ge=0, strict=True)


class CandidateObservation(StrictModel):
    """Analyzer-observed soft-score inputs for one candidate (integers only)."""

    candidate_id: Sha256
    kind: SegmentKind
    content_score: int = Field(ge=0, le=10, strict=True)
    clarity_score: int = Field(ge=0, le=10, strict=True)

    def weighted_score(self, weights: ScoreWeights) -> int:
        return weights.content_weight * self.content_score + (
            weights.clarity_weight * self.clarity_score
        )


class DeclaredOrderLock(StrictModel):
    """Locked ordering relation: before must precede after when both selected."""

    before_candidate_id: Sha256
    after_candidate_id: Sha256
    basis: str = Field(min_length=1, strict=True)

    @model_validator(mode="after")
    def require_distinct_endpoints(self) -> DeclaredOrderLock:
        if self.before_candidate_id == self.after_candidate_id:
            raise PydanticCustomError(
                "lock_self", "an order lock must relate two distinct candidates"
            )
        return self


class PlannerInput(StrictModel):
    """The committed selection plan fields plus solver-side frozen facts."""

    schema_version: Literal["planner-input-v1"] = PLANNER_INPUT_SCHEMA_VERSION
    episode_id: Identifier
    candidates: tuple[Candidate, ...] = Field(min_length=1)
    must_include: tuple[Sha256, ...] = ()
    budget: DurationBudgetRules
    ordering: OrderingRules
    weights: ScoreWeights
    observations: tuple[CandidateObservation, ...]
    order_locks: tuple[DeclaredOrderLock, ...] = ()
    capability_allowlist: tuple[str, ...] = Field(min_length=1)
    edit_source: EditSourceFacts

    @model_validator(mode="after")
    def require_unique_identities(self) -> PlannerInput:
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("duplicate_identity", "candidate identities are unique")
        if len(set(self.must_include)) != len(self.must_include):
            raise PydanticCustomError("duplicate_must", "must_include ids must be unique")
        return self

    @model_validator(mode="after")
    def require_observation_coverage(self) -> PlannerInput:
        known = {candidate.candidate_id for candidate in self.candidates}
        observed = [row.candidate_id for row in self.observations]
        if len(set(observed)) != len(observed):
            raise PydanticCustomError("duplicate_observation", "observation ids are unique")
        unknown = sorted(set(observed) - known)
        if unknown:
            raise PydanticCustomError(
                "unknown_observation",
                "observations reference unknown candidates: {unknown}",
                {"unknown": str(unknown)},
            )
        keeps = {
            candidate.candidate_id
            for candidate in self.candidates
            if candidate.intent == "keep"
        }
        missing = sorted(keeps - set(observed))
        if missing:
            raise PydanticCustomError(
                "unobserved_keep",
                "every keep candidate needs observation facts: {missing}",
                {"missing": str(missing)},
            )
        return self


class AllocatedSpan(StrictModel):
    """One selected candidate with its exact integer record allocation."""

    candidate_id: Sha256
    source_span: CandidateSpan
    record_start_frame: Frame
    record_end_frame: Frame


class SoftScoreBreakdown(StrictModel):
    """Per-candidate soft score components and the declared-weight total."""

    candidate_id: Sha256
    kind: SegmentKind
    content_score: int = Field(ge=0, le=10, strict=True)
    clarity_score: int = Field(ge=0, le=10, strict=True)
    content_weight: int = Field(ge=0, strict=True)
    clarity_weight: int = Field(ge=0, strict=True)
    weighted_score: int = Field(ge=0, strict=True)


class HandlePassThrough(StrictModel):
    """Story/section handles passed through verbatim into the ordering trace."""

    candidate_id: Sha256
    head_frames: int = Field(ge=0, strict=True)
    tail_frames: int = Field(ge=0, strict=True)


class BudgetDropTrace(StrictModel):
    """One budget-enforced drop, in chronological drop order."""

    candidate_id: Sha256
    weighted_score: int = Field(ge=0, strict=True)
    reason_code: Literal["budget-dropped"] = "budget-dropped"


class OrderingTrace(StrictModel):
    """The deterministic ordering decisions the solver made."""

    rule_id: str = Field(min_length=1, strict=True)
    rule: str = Field(min_length=1, strict=True)
    tie_break: Literal["lexicographic-candidate-id"] = "lexicographic-candidate-id"
    handles: tuple[HandlePassThrough, ...] = ()
    budget_dropped: tuple[BudgetDropTrace, ...] = ()


class PlannerSolution(StrictModel):
    """A feasible solve: selected ids, exact integer spans, scores, trace."""

    status: Literal["solved"] = "solved"
    episode_id: Identifier
    ordering_rule_id: str = Field(min_length=1, strict=True)
    selected_candidate_ids: tuple[Sha256, ...]
    allocated: tuple[AllocatedSpan, ...]
    total_duration_frames: int = Field(ge=0, strict=True)
    scores: tuple[SoftScoreBreakdown, ...]
    ordering_trace: OrderingTrace

    @model_validator(mode="after")
    def require_consistent_allocation(self) -> PlannerSolution:
        allocated_ids = [row.candidate_id for row in self.allocated]
        if allocated_ids != list(self.selected_candidate_ids):
            raise PydanticCustomError(
                "allocation_order", "allocated spans follow the selected order"
            )
        if [row.candidate_id for row in self.scores] != allocated_ids:
            raise PydanticCustomError(
                "score_order", "score breakdowns follow the selected order"
            )
        cursor = 0
        for row in self.allocated:
            if row.record_start_frame != cursor or row.record_end_frame <= cursor:
                raise PydanticCustomError(
                    "allocation_contiguous",
                    "record allocations are contiguous half-open frames from zero",
                )
            cursor = row.record_end_frame
        if cursor != self.total_duration_frames:
            raise PydanticCustomError(
                "allocation_total", "total_duration_frames equals the allocated sum"
            )
        return self


class InfeasibilityReport(StrictModel):
    """A typed infeasibility report — NO plan is ever emitted alongside it."""

    status: Literal["infeasible"] = "infeasible"
    kind: InfeasibilityKind
    constraint: str = Field(min_length=1, strict=True)
    offending_candidate_ids: tuple[Sha256, ...]
    explanation: str = Field(min_length=1, strict=True)
    suggestions: tuple[str, ...] = ()


type PlannerOutcome = PlannerSolution | InfeasibilityReport


__all__ = [
    "PLANNER_INPUT_SCHEMA_VERSION",
    "AllocatedSpan",
    "BudgetDropTrace",
    "CandidateObservation",
    "DeclaredOrderLock",
    "HandlePassThrough",
    "InfeasibilityKind",
    "InfeasibilityReport",
    "OrderingTrace",
    "PlannerInput",
    "PlannerOutcome",
    "PlannerSolution",
    "ScoreWeights",
    "SoftScoreBreakdown",
]
