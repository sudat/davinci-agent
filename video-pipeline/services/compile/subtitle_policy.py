"""Subtitle QC policy and transcript cue-source models (Todo 44).

The policy is the single declared source for subtitle limits (minimum display
duration, line count, characters per line) and the closed set of style ids —
cue generation may only reference declared styles (Phase 3 introduces real
styles; Phase 1 carries one fixed default ref).

Transcript cue sources are the DECLARED subtitle inputs: half-open spans on
the edit-source frame axis or on the edit-source audio sample axis. Floats
cannot be expressed anywhere.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Frame, Identifier, PositiveInteger, StrictModel

CueSpan = Annotated[
    "FrameCueSpan | SampleCueSpan",
    Field(union_mode="smart"),
]


class FrameCueSpan(StrictModel):
    """Half-open cue span on the edit-source video frame axis."""

    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> FrameCueSpan:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError(
                "span_empty", "cue spans are non-empty half-open ranges"
            )
        return self


class SampleCueSpan(StrictModel):
    """Half-open cue span on the edit-source audio sample axis."""

    start_sample: Frame
    end_sample: Frame
    sample_rate: PositiveInteger

    @model_validator(mode="after")
    def require_forward(self) -> SampleCueSpan:
        if self.end_sample <= self.start_sample:
            raise PydanticCustomError(
                "span_empty", "cue spans are non-empty half-open ranges"
            )
        return self


class TranscriptCueSegment(StrictModel):
    """One declared dialogue cue: text plus its source span."""

    segment_id: Identifier
    text: str = Field(min_length=1, strict=True)
    span: CueSpan


class TranscriptCueSource(StrictModel):
    """The declared transcript cue table for one episode (Japanese only)."""

    language: Literal["ja"]
    segments: tuple[TranscriptCueSegment, ...] = ()

    @model_validator(mode="after")
    def require_unique_segment_ids(self) -> TranscriptCueSource:
        ids = [segment.segment_id for segment in self.segments]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError(
                "duplicate_segment", "transcript cue segment ids are unique"
            )
        return self


class SubtitleQcPolicy(StrictModel):
    """Frozen subtitle limits plus the closed declared style set."""

    policy_id: Identifier
    min_duration_frames: int = Field(gt=0, strict=True)
    max_lines: int = Field(gt=0, strict=True)
    max_chars_per_line: int = Field(gt=0, strict=True)
    declared_style_refs: tuple[Identifier, ...] = Field(min_length=1)
    default_style_ref: Identifier

    @model_validator(mode="after")
    def require_declared_default_style(self) -> SubtitleQcPolicy:
        if len(set(self.declared_style_refs)) != len(self.declared_style_refs):
            raise PydanticCustomError(
                "duplicate_style_ref", "declared style refs are unique"
            )
        if self.default_style_ref not in self.declared_style_refs:
            raise PydanticCustomError(
                "style_ref_undeclared",
                "default_style_ref {ref} must be a declared style id",
                {"ref": self.default_style_ref},
            )
        return self


__all__ = [
    "CueSpan",
    "FrameCueSpan",
    "SampleCueSpan",
    "SubtitleQcPolicy",
    "TranscriptCueSegment",
    "TranscriptCueSource",
]
