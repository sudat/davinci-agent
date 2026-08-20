"""Wrapped event streams and summary records for the metrics bundle.

Review and stage events are the REAL recorded formats (phase-0C review
events, stage-journal events), each line tagged with the episode it
belongs to. QC outcomes and the artifact inventory are summary records
of the qc/build reports and the artifact registry; trace records link
Decisions to Sources, Review, and Build items (PRD 31.6).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel
from services.job_runner.stage_runner_models import (
    StageJournalEvent,
)
from services.metrics.models import Seq  # noqa: TC001 (pydantic resolves Seq at runtime)
from services.review_command.events import (
    ReviewEvent0C,
    event_proposal,
)

EDIT_COMMAND_KINDS = frozenset(
    {"remove_segment", "adjust_source_span", "correct_subtitle"}
)


class WrappedReviewEvent(StrictModel):
    episode_id: Identifier
    event: ReviewEvent0C


class WrappedStageEvent(StrictModel):
    episode_id: Identifier
    event: StageJournalEvent


class QcOutcomeRecord(StrictModel):
    """Outcome summary of one QC report (verdict + issue counts)."""

    episode_id: Identifier
    verdict: Literal["passed", "blocked"]
    blocker_count: int = Field(ge=0, strict=True)
    major_count: int = Field(ge=0, strict=True)
    minor_count: int = Field(ge=0, strict=True)


RetentionPolicyClass = Literal[
    "authoritative", "rebuildable", "runtime_cache", "manual_finalization"
]


class ArtifactInventoryRecord(StrictModel):
    """One registry artifact with its retention class and byte size."""

    episode_id: Identifier
    artifact_id: Identifier
    retention_class: RetentionPolicyClass
    byte_size: int = Field(ge=0, strict=True)


class TraceSource(StrictModel):
    episode_id: Identifier
    source_id: Identifier


class TraceDecision(StrictModel):
    episode_id: Identifier
    decision_id: Identifier
    source_span_ids: Seq[Identifier] = ()

    @model_validator(mode="after")
    def require_unique_spans(self) -> TraceDecision:
        if len(set(self.source_span_ids)) != len(self.source_span_ids):
            raise PydanticCustomError(
                "duplicate_span", "source span ids must be unique per decision"
            )
        return self


class TraceBuildItem(StrictModel):
    episode_id: Identifier
    item_id: Identifier
    decision_id: Identifier


def is_correction_event(wrapped: WrappedReviewEvent) -> bool:
    """True for applied EDIT commands (approval events are not corrections)."""

    if wrapped.event.kind != "decision_applied":
        return False
    return event_proposal(wrapped.event).command_kind in EDIT_COMMAND_KINDS


__all__ = [
    "EDIT_COMMAND_KINDS",
    "ArtifactInventoryRecord",
    "QcOutcomeRecord",
    "RetentionPolicyClass",
    "StageJournalEvent",
    "TraceBuildItem",
    "TraceDecision",
    "TraceSource",
    "WrappedReviewEvent",
    "WrappedStageEvent",
    "is_correction_event",
]
