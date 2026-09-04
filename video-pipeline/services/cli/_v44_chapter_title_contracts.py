"""Strict wire contracts for the v4.4 chapter-title runtime sidecar."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Frame,
    Identifier,
    RecordFrameSpan,
    Sha256,
    StrictModel,
    to_tuple,
)
from services.creative_plan.presentation_intents import (
    ChapterCardParams,
    PresentationIntentV2,
)

_JAPANESE_CHARACTER: Final = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
_Title = Annotated[str, Field(min_length=1, strict=True)]
_ThreeTitles = Annotated[
    tuple[_Title, _Title, _Title],
    BeforeValidator(to_tuple),
]


class ChapterBoundaryBinding(StrictModel):
    left_item_id: Identifier
    right_item_id: Identifier
    record_frame: Frame
    record_seconds: float = Field(ge=0.0, strict=True)
    source_frame: Frame


class SubtitleEvidenceCue(StrictModel):
    subtitle_id: Identifier
    source_start_frame: Frame
    source_end_frame: Frame
    text: _Title

    @model_validator(mode="after")
    def require_forward_span(self) -> SubtitleEvidenceCue:
        if self.source_end_frame <= self.source_start_frame:
            raise PydanticCustomError(
                "evidence-span-empty", "subtitle evidence spans must be non-empty"
            )
        return self


class SubtitleEvidenceSet(StrictModel):
    evidence_role: Literal["untrusted_data"] = "untrusted_data"
    cues: tuple[SubtitleEvidenceCue, ...] = Field(min_length=1)


class ChapterTitleModelRequest(StrictModel):
    schema_version: Literal["v44-chapter-title-request-v1"] = (
        "v44-chapter-title-request-v1"
    )
    task: Literal["propose_japanese_chapter_titles"] = (
        "propose_japanese_chapter_titles"
    )
    language: Literal["ja"] = "ja"
    boundary: ChapterBoundaryBinding
    evidence_role: Literal["untrusted_data"] = "untrusted_data"
    subtitle_evidence: tuple[SubtitleEvidenceCue, ...] = Field(min_length=1)


class ChapterTitleModelResponse(StrictModel):
    titles: _ThreeTitles

    @field_validator("titles")
    @classmethod
    def require_unique_japanese_titles(
        cls, titles: tuple[str, str, str]
    ) -> tuple[str, str, str]:
        if any(title != title.strip() or "\n" in title or "\r" in title for title in titles):
            raise PydanticCustomError(
                "chapter-title-format", "chapter titles must be single trimmed lines"
            )
        if len(set(titles)) != len(titles):
            raise PydanticCustomError(
                "chapter-title-duplicate", "the three chapter titles must be distinct"
            )
        if any(_JAPANESE_CHARACTER.search(title) is None for title in titles):
            raise PydanticCustomError(
                "chapter-title-language", "each chapter title must contain Japanese text"
            )
        return titles


class EvidenceBinding(StrictModel):
    sha256: Sha256
    cue_count: int = Field(gt=0, strict=True)
    first_subtitle_id: Identifier
    last_subtitle_id: Identifier


class ModelPinBinding(StrictModel):
    mode: Literal["production_model"]
    transport: Literal["codex-exec"]
    purpose: Literal["editorial-director-v2"]
    model_id: Literal["gpt-5.6-sol"]
    runtime_sha256: Sha256
    director_pin_sha256: Sha256


class PendingChapterTitleCandidate(StrictModel):
    candidate_id: Sha256
    presentation_intent: PresentationIntentV2


class ChapterTitleProposalSidecar(StrictModel):
    schema_version: Literal["v44-chapter-title-proposal-sidecar-v1"] = (
        "v44-chapter-title-proposal-sidecar-v1"
    )
    proposal_only: Literal[True] = True
    approval_status: Literal["pending"] = "pending"
    episode_id: Identifier
    base_plan_version: str = Field(pattern=r"^v[1-9][0-9]*$", strict=True)
    base_plan_sha256: Sha256
    base_ir_sha256: Sha256
    boundary: ChapterBoundaryBinding
    evidence: EvidenceBinding
    model_pin: ModelPinBinding
    chapter_card_duration_frames: Literal[45] = 45
    candidates: tuple[
        PendingChapterTitleCandidate,
        PendingChapterTitleCandidate,
        PendingChapterTitleCandidate,
    ]


@dataclass(frozen=True, slots=True)
class CandidateContext:
    episode_id: str
    plan_sha256: str
    boundary: ChapterBoundaryBinding


def _candidate(title: str, context: CandidateContext) -> PendingChapterTitleCandidate:
    identity_bytes = b"\0".join(
        (
            b"v44-chapter-title-candidate-v1",
            context.episode_id.encode(),
            context.plan_sha256.encode(),
            str(context.boundary.record_frame).encode(),
            title.encode(),
        )
    )
    candidate_id = hashlib.sha256(identity_bytes).hexdigest()
    return PendingChapterTitleCandidate(
        candidate_id=candidate_id,
        presentation_intent=PresentationIntentV2(
            intent_id=candidate_id,
            kind="chapter_card",
            target_span=RecordFrameSpan(
                start_frame=context.boundary.record_frame,
                end_frame=context.boundary.record_frame + 45,
            ),
            params=ChapterCardParams(title=title, duration_frames=45),
            rationale="operator-selected chapter boundary; title remains pending",
        ),
    )


def build_candidates(
    titles: tuple[str, str, str], context: CandidateContext
) -> tuple[
    PendingChapterTitleCandidate,
    PendingChapterTitleCandidate,
    PendingChapterTitleCandidate,
]:
    return (
        _candidate(titles[0], context),
        _candidate(titles[1], context),
        _candidate(titles[2], context),
    )


__all__ = [
    "CandidateContext",
    "ChapterBoundaryBinding",
    "ChapterTitleModelRequest",
    "ChapterTitleModelResponse",
    "ChapterTitleProposalSidecar",
    "EvidenceBinding",
    "ModelPinBinding",
    "SubtitleEvidenceCue",
    "SubtitleEvidenceSet",
    "build_candidates",
]
