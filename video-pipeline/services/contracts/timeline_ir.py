from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    ArtifactEnvelope,
    AvLinkId,
    Identifier,
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
from services.contracts.styled_presentation import (  # noqa: TC001 (pydantic runtime)
    StyledPresentation,
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


class TimelineGapItem(ResolveFreeModel):
    """An explicit record-range gap: no source, legal on any track (Todo 44)."""

    kind: Literal["gap"] = "gap"
    item_id: ItemId
    record_span: RecordFrameSpan


class SubtitleCueItem(ResolveFreeModel):
    """One NLE-neutral subtitle cue with its frozen presentation metadata."""

    kind: Literal["subtitle_cue"] = "subtitle_cue"
    item_id: ItemId
    source: SourceRef
    record_span: RecordFrameSpan
    text: str = Field(min_length=1, strict=True)
    lines: tuple[str, ...] = Field(min_length=1)
    style_ref: Identifier
    safe_area: bool
    min_duration_frames: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_non_empty_lines(self) -> SubtitleCueItem:
        if any(not line for line in self.lines):
            raise PydanticCustomError("cue_lines", "cue lines are non-empty")
        if self.text.strip() != self.text:
            raise PydanticCustomError("cue_text", "cue text carries no surrounding space")
        return self


ProductionItem = Annotated[
    TimelineItem0C | TimelineGapItem | SubtitleCueItem,
    Field(discriminator="kind"),
]


class TimelineTrackProduction(ResolveFreeModel):
    """One logical production track over the extended item union."""

    track: TrackRef0C
    items: tuple[ProductionItem, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_matching_item_kinds(self) -> TimelineTrackProduction:
        for item in self.items:
            if item.kind == "gap":
                continue
            expected: str = (
                "subtitle"
                if item.kind == "subtitle_cue"
                else item.kind  # TimelineItem0C kinds are already track kinds
            )
            if expected != self.track.kind:
                raise PydanticCustomError(
                    "track_kind_mismatch",
                    "item kind {kind} does not match track {track}",
                    {"kind": item.kind, "track": self.track.kind},
                )
        return self


class TimelineIrProduction(ResolveFreeEnvelope[Literal["timeline_ir_v1"]]):
    """The production Timeline IR: NLE-neutral placements + subtitle cues.

    ``presentation`` carries the Phase-3 styled output (styled cues + titled
    items, NLE-neutral style parameters and asset content refs) attached
    AFTER the editorial compile; it never participates in the editorial
    ``content_hash``, which stays computed over the tracks alone so profile
    swaps cannot perturb editorial identity.
    """

    rate: RationalFrameRate
    tracks: tuple[TimelineTrackProduction, ...] = Field(min_length=1)
    presentation: StyledPresentation | None = None

    @model_validator(mode="after")
    def require_unique_track_refs(self) -> TimelineIrProduction:
        refs = [(track.track.kind, track.track.index) for track in self.tracks]
        if len(set(refs)) != len(refs):
            raise PydanticCustomError(
                "duplicate_track", "each logical track appears at most once"
            )
        return self
