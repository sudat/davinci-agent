"""Strict Edit Plan models: video/audio items, link groups, decisions (Todo 43).

An Edit Plan is NLE-independent: Resolve-specific field names are rejected at
the model level (Todo 26 precedent) and LLM-authored ``llm_uuid``/
``decision_uuid`` keys are refused before construction — decision identity is
recomputed from the planner inputs, never carried across from a model (PRD
14.2). Video and audio items are separate and linked: the VIDEO item id
anchors the link group of its audio counterpart (PRD 14.1). Field locks are
per the PRD 14.3 lock model; every item carries provenance; record spans are
the planner's integer timeline allocation (contiguous from zero).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    ArtifactId,
    Frame,
    Identifier,
    ResolveFreeModel,
    Sha256,
    StrictModel,
    TrackKind,
)
from services.editorial.candidate_models import (  # noqa: TC001 (pydantic runtime fields)
    CandidateSourceRef,
    CandidateSpan,
    ProposalProducer,
)
from services.plan.edit_plan_ids import (
    compute_decision_id,
    compute_edit_item_id,
    offending_uuid_key,
)
from services.validate.selection_models import (  # noqa: TC001 (pydantic runtime fields)
    SelectionLockField,
)

type EditDecisionKind = Literal["keep", "remove", "adjust"]

EDIT_PLAN_SCHEMA_VERSION: Literal["edit-plan-v1"] = "edit-plan-v1"


class _LlmUuidFreeModel(ResolveFreeModel):
    """Model-level rejection of Resolve fields AND LLM-authored uuid keys."""

    @model_validator(mode="before")
    @classmethod
    def reject_llm_uuid_fields(cls, value: object) -> object:
        offending = offending_uuid_key(value)
        if offending is not None:
            raise PydanticCustomError(
                "llm_uuid_forbidden",
                "LLM-authored uuid fields are never trusted: {key}",
                {"key": offending},
            )
        return value


class RecordSpan(StrictModel):
    """Half-open integer record (timeline placement) span."""

    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> RecordSpan:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError(
                "record_span_empty", "record spans are non-empty half-open ranges"
            )
        return self

    @property
    def length(self) -> int:
        return self.end_frame - self.start_frame


class SelectionPlanRef(StrictModel):
    """Identity of the committed Selection Plan version an Edit Plan builds on."""

    episode_id: Identifier
    plan_version: str = Field(pattern=r"^v[1-9][0-9]*$", strict=True)
    plan_sha256: Sha256
    plan_artifact_id: ArtifactId


class AudioSpanBinding(StrictModel):
    """Declared audio placement for one candidate's A/V link (offsets allowed)."""

    candidate_id: Sha256
    span: CandidateSpan


class EditItemProvenance(StrictModel):
    candidate_id: Sha256
    analyzer: str = Field(min_length=1, strict=True)
    rule: str = Field(min_length=1, strict=True)
    parent_candidate_id: Sha256 | None = None


class EditPlanDecision(_LlmUuidFreeModel):
    """One deterministic editorial decision over a selection candidate."""

    decision_id: Sha256
    kind: EditDecisionKind
    candidate_id: Sha256
    parent_candidate_id: Sha256 | None = None
    rule: str = Field(min_length=1, strict=True)


class EditPlanItem(_LlmUuidFreeModel):
    """One placed video or audio item with its deterministic identity."""

    item_id: Sha256
    track_kind: TrackKind
    link_group_id: Sha256
    source_ref: CandidateSourceRef
    span: CandidateSpan
    record_span: RecordSpan
    decision_id: Sha256
    locks: tuple[SelectionLockField, ...] = ()
    provenance: EditItemProvenance

    @model_validator(mode="after")
    def require_derived_identity(self) -> EditPlanItem:
        expected = compute_edit_item_id(
            source_id=self.source_ref.source_id,
            edit_source_sha=self.source_ref.edit_source_sha,
            span=self.span,
            track_kind=self.track_kind,
            decision_id=self.decision_id,
        )
        if self.item_id != expected:
            raise PydanticCustomError(
                "identity_binding", "item_id must equal the deterministic identity hash"
            )
        if self.track_kind == "video" and self.link_group_id != self.item_id:
            raise PydanticCustomError(
                "link_anchor", "a video item anchors its own link group"
            )
        if self.track_kind == "audio" and self.link_group_id == self.item_id:
            raise PydanticCustomError(
                "link_anchor", "an audio item must reference a video item's link group"
            )
        if len(set(self.locks)) != len(self.locks):
            raise PydanticCustomError("duplicate_lock", "item locks are unique fields")
        return self


class EditPlan(_LlmUuidFreeModel):
    """The reconciled edit plan proposal; commit authority is Todo 43's commit."""

    schema_version: Literal["edit-plan-v1"] = EDIT_PLAN_SCHEMA_VERSION
    proposal_id: Identifier
    episode_id: Identifier
    plan_base_version: Identifier
    base_selection_plan: SelectionPlanRef
    items: tuple[EditPlanItem, ...] = Field(min_length=1)
    decisions: tuple[EditPlanDecision, ...] = Field(min_length=1)
    total_duration_frames: int = Field(gt=0, strict=True)
    producer: ProposalProducer
    fixture_only: bool

    @model_validator(mode="after")
    def require_structural_integrity(self) -> EditPlan:
        _require_unique_identities(self)
        _require_decision_identities(self)
        _require_av_links(self)
        _require_record_layout(self)
        return self


def _require_unique_identities(plan: EditPlan) -> None:
    item_ids = [item.item_id for item in plan.items]
    if len(set(item_ids)) != len(item_ids):
        raise PydanticCustomError("duplicate_item", "plan item ids must be unique")
    rows = [row.candidate_id for row in plan.decisions]
    if len(set(rows)) != len(rows):
        raise PydanticCustomError(
            "duplicate_decision", "one decision per selection candidate"
        )


def _require_decision_identities(plan: EditPlan) -> None:
    ref = plan.base_selection_plan
    kinds = {row.candidate_id: row.kind for row in plan.decisions}
    for row in plan.decisions:
        expected = compute_decision_id(
            base_episode_id=ref.episode_id,
            base_plan_version=ref.plan_version,
            base_plan_sha256=ref.plan_sha256,
            candidate_id=row.candidate_id,
            kind=row.kind,
            parent_candidate_id=row.parent_candidate_id,
        )
        if row.decision_id != expected:
            raise PydanticCustomError(
                "decision_identity",
                "decision ids recompute from the base selection plan",
            )
    per_decision: dict[str, int] = {}
    for item in plan.items:
        if item.track_kind != "video":
            continue
        per_decision[item.decision_id] = per_decision.get(item.decision_id, 0) + 1
    for decision_id, count in per_decision.items():
        if count != 1:
            raise PydanticCustomError(
                "decision_items", "each placed decision anchors exactly one video item"
            )
        if kinds.get(decision_id) == "remove":
            raise PydanticCustomError("remove_has_items", "remove decisions place no items")


def _require_av_links(plan: EditPlan) -> None:
    videos = {item.item_id: item for item in plan.items if item.track_kind == "video"}
    audios = [item for item in plan.items if item.track_kind == "audio"]
    anchors = [audio.link_group_id for audio in audios]
    if len(set(anchors)) != len(anchors):
        raise PydanticCustomError(
            "av_link_broken", "every video item has exactly one audio counterpart"
        )
    for audio in audios:
        anchor = videos.get(audio.link_group_id)
        if (
            anchor is None
            or anchor.decision_id != audio.decision_id
            or anchor.record_span != audio.record_span
            or anchor.span.length != audio.span.length
        ):
            raise PydanticCustomError(
                "av_link_broken",
                "audio item {item} does not resolve to its video counterpart",
                {"item": audio.item_id},
            )
    if set(anchors) != set(videos):
        raise PydanticCustomError(
            "av_link_broken", "every video item has exactly one audio counterpart"
        )


def _require_record_layout(plan: EditPlan) -> None:
    cursor = 0
    for video in (item for item in plan.items if item.track_kind == "video"):
        if video.record_span.start_frame != cursor:
            raise PydanticCustomError(
                "record_contiguous", "video record spans are contiguous from zero"
            )
        if video.record_span.length != video.span.length:
            raise PydanticCustomError(
                "record_length", "record spans mirror the source span length"
            )
        cursor = video.record_span.end_frame
    if cursor != plan.total_duration_frames:
        raise PydanticCustomError(
            "total_duration", "total_duration_frames equals the record sum"
        )


__all__ = [
    "EDIT_PLAN_SCHEMA_VERSION",
    "AudioSpanBinding",
    "EditDecisionKind",
    "EditItemProvenance",
    "EditPlan",
    "EditPlanDecision",
    "EditPlanItem",
    "RecordSpan",
    "SelectionPlanRef",
]
