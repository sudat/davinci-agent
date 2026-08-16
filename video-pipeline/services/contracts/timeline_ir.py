from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    ArtifactEnvelope,
    AvLinkId,
    ItemId,
    RationalFrameRate,
    RecordFrameSpan,
    ResolveFreeEnvelope,
    ResolveFreeModel,
    SourceRef,
    StrictModel,
    TrackKind,
    TrackRef,
)


class TimelineItem0A(StrictModel):
    item_id: ItemId
    kind: TrackKind
    source: SourceRef
    record_span: RecordFrameSpan
    av_link_id: AvLinkId | None


class TimelineTrack0A(StrictModel):
    track: TrackRef
    items: tuple[TimelineItem0A, ...]

    @model_validator(mode="after")
    def require_matching_item_kinds(self) -> TimelineTrack0A:
        if any(item.kind != self.track.kind for item in self.items):
            raise PydanticCustomError(
                "track_kind_mismatch",
                "every item kind must match its containing track",
            )
        return self


class TimelineIr0A(ArtifactEnvelope[Literal["timeline_ir_0a"]]):
    rate: RationalFrameRate
    tracks: tuple[TimelineTrack0A, ...]


class TrackRef0C(ResolveFreeModel):
    kind: Literal["video", "audio", "subtitle"]
    index: int = Field(gt=0, strict=True)


class TimelineItem0C(ResolveFreeModel):
    item_id: ItemId
    kind: Literal["video", "audio", "subtitle"]
    source: SourceRef
    record_span: RecordFrameSpan
    av_link_id: AvLinkId | None = None
    subtitle_text: str | None = None

    @model_validator(mode="after")
    def require_kind_consistency(self) -> TimelineItem0C:
        if self.kind == "subtitle":
            if self.subtitle_text is None or not self.subtitle_text:
                raise PydanticCustomError("subtitle_text", "subtitle items must carry text")
        elif self.subtitle_text is not None:
            raise PydanticCustomError("subtitle_text", "only subtitle items carry text")
        if self.kind not in ("video", "audio") and self.av_link_id is not None:
            raise PydanticCustomError("av_link", "only A/V items may carry a link id")
        return self


class TimelineTrack0C(ResolveFreeModel):
    track: TrackRef0C
    items: tuple[TimelineItem0C, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_matching_item_kinds(self) -> TimelineTrack0C:
        if any(item.kind != self.track.kind for item in self.items):
            raise PydanticCustomError(
                "track_kind_mismatch",
                "every item kind must match its containing track",
            )
        return self


class TimelineIr0C(ResolveFreeEnvelope[Literal["timeline_ir_0c"]]):
    rate: RationalFrameRate
    tracks: tuple[TimelineTrack0C, ...] = Field(min_length=1)
