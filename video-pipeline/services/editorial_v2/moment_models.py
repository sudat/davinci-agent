"""MomentCandidateV2 — multimodal moment-candidate model (task 22).

14 candidate types (PRD 8.10) plus decision fields:
source span (strict-int), story block, intent, rationale,
evidence refs (min_length=1), redundancy group, confidence,
handles (strict-int), lock state, provenance. Selection proposal
enforces keep-count sanity and non-empty evidence.

Coordinate contract: source_span frames are Edit Source Frames
(CFR Edit Mezzanine) — strict integer via ``Frame``.

Tuple coercion: every ``tuple`` field carries ``BeforeValidator(_to_tuple)``
so JSON lists round-trip through ``model_validate`` (cf. tasks 12/24).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Frame, Identifier, StrictModel


def _to_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


MomentCandidateType = Literal[
    "speech",
    "reaction",
    "action",
    "establishing",
    "b_roll",
    "insert",
    "product_demo",
    "screen_demo",
    "ambient",
    "transition",
    "graphic",
    "still",
    "pause",
    "alternate_take",
]

MomentIntent = Literal["keep", "remove", "optional"]
MomentLockState = Literal["unlocked", "locked"]


class MomentSourceSpan(StrictModel):
    """Half-open ``[start_frame, end_frame)`` on the Edit Source."""

    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward_span(self) -> MomentSourceSpan:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError(
                "span_empty",
                "source_span is a non-empty half-open range",
            )
        return self


class MomentHandles(StrictModel):
    """Head/tail handles around the source span."""

    in_frame: Frame = 0
    out_frame: Frame = 0


class MomentProvenance(StrictModel):
    """Provenance for a moment candidate."""

    producer: Annotated[str, Field(min_length=1, strict=True)]
    version: Annotated[str, Field(min_length=1, strict=True)] | None = None


class MomentCandidateV2(StrictModel):
    """One multimodal moment candidate with decision fields."""

    candidate_id: Identifier
    candidate_type: MomentCandidateType
    source_span: MomentSourceSpan
    story_block_ref: Identifier | None = None
    intent: MomentIntent
    rationale: Annotated[str, Field(min_length=1, strict=True)]
    evidence_refs: Annotated[
        tuple[Identifier, ...], BeforeValidator(_to_tuple)
    ] = Field(min_length=1)
    redundancy_group: Identifier | None = None
    confidence: Annotated[float, Field(ge=0, le=1)]
    handles: MomentHandles | None = None
    lock_state: MomentLockState = "unlocked"
    provenance: MomentProvenance


class MomentSelectionProposalV2(StrictModel):
    """Proposal envelope for moment selection."""

    proposal_id: Identifier
    episode_id: Identifier
    candidates: Annotated[
        tuple[MomentCandidateV2, ...], BeforeValidator(_to_tuple)
    ] = Field(min_length=1)

    @model_validator(mode="after")
    def require_keep_and_evidence(self) -> MomentSelectionProposalV2:
        keep_count = sum(1 for c in self.candidates if c.intent == "keep")
        if keep_count < 1:
            raise PydanticCustomError(
                "keep_count",
                "proposal must contain at least one keep candidate",
            )
        for cand in self.candidates:
            if len(cand.evidence_refs) < 1:
                raise PydanticCustomError(
                    "evidence_empty",
                    "each candidate must carry at least one evidence ref",
                )
        return self


__all__ = [
    "MomentCandidateType",
    "MomentCandidateV2",
    "MomentHandles",
    "MomentIntent",
    "MomentLockState",
    "MomentProvenance",
    "MomentSelectionProposalV2",
    "MomentSourceSpan",
]
