from __future__ import annotations

from typing import Literal

from pydantic import model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    ArtifactEnvelope,
    AvLinkId,
    ItemId,
    RationalFrameRate,
    RecordFrameSpan,
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
