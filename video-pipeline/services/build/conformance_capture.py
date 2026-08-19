"""Readback capture: strict observation of every placed timeline item.

``capture_readback`` walks a timeline through public track/item APIs and
strict-coerces every frame field (source spans are observed as
StartFrame + Duration; the API's end-frame accessor is deliberately unused
because of its live-verified float-floor artifact). The resulting
:class:`TimelineReadback` table is the sole observed input to conformance
verification and drift detection — both offline fakes and the live bridge
produce the same strict shape.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.build.builder_models import BuildFailure
from services.contracts.primitives import Sha256, StrictModel, TrackKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from services.resolve_bridge.base_cut_models import (
        BaseCutTimelineApi,
        BaseCutTimelineItemApi,
    )

READ_KINDS: Final[tuple[str, ...]] = ("video", "audio")
PLACEMENT_FIELDS: Final = 2
type Sha256Of = Callable[[Path], str]


class ReadbackRow(StrictModel):
    """One observed timeline item; the source span is StartFrame + Duration."""

    unique_id: str = Field(min_length=1)
    kind: TrackKind
    track_index: int = Field(gt=0, strict=True)
    record_start: int = Field(ge=0, strict=True)
    record_end: int = Field(gt=0, strict=True)
    source_start: int = Field(ge=0, strict=True)
    source_duration: int = Field(gt=0, strict=True)
    media_path: str = Field(min_length=1)
    media_sha256: Sha256
    linked_ids: tuple[str, ...] = Field(default=())

    @model_validator(mode="after")
    def require_forward_spans(self) -> ReadbackRow:
        if self.record_end <= self.record_start:
            raise PydanticCustomError("span", "record span must be forward")
        return self


class TimelineReadback(StrictModel):
    timeline_name: str = Field(min_length=1)
    rows: tuple[ReadbackRow, ...]

    @model_validator(mode="after")
    def require_unique_ids(self) -> TimelineReadback:
        ids = [row.unique_id for row in self.rows]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("unique_id", "duplicate unique ids in readback")
        return self


def as_frame(value: float, what: str) -> int:
    """Strict integer-frame coercion; fractional or non-numeric readback fails."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BuildFailure(
            "readback-mismatch", f"{what} returned a non-numeric value: {value!r}"
        )
    if isinstance(value, float):
        if not value.is_integer():
            raise BuildFailure(
                "readback-mismatch", f"{what} returned a fractional frame: {value!r}"
            )
        return int(value)
    return int(value)


def capture_readback(timeline: BaseCutTimelineApi, sha256_of: Sha256Of) -> TimelineReadback:
    """Read every placed item back through public timeline APIs."""

    rows: list[ReadbackRow] = []
    for kind in READ_KINDS:
        for index in range(1, timeline.GetTrackCount(kind) + 1):
            rows.extend(
                _capture_row(item, sha256_of)
                for item in timeline.GetItemListInTrack(kind, index) or ()
            )
    return TimelineReadback(timeline_name=timeline.GetName(), rows=tuple(rows))


def _capture_row(item: BaseCutTimelineItemApi, sha256_of: Sha256Of) -> ReadbackRow:
    uid = item.GetUniqueId()
    what = f"timeline item {uid!r}"
    record_start = as_frame(item.GetStart(False), f"{what} record start")  # noqa: FBT003 (API flag)
    record_end = as_frame(item.GetEnd(False), f"{what} record end")  # noqa: FBT003 (API flag)
    source_start = as_frame(item.GetSourceStartFrame(), f"{what} source start")
    duration = as_frame(item.GetDuration(False), f"{what} duration")  # noqa: FBT003 (API flag)
    track = item.GetTrackTypeAndIndex()
    if len(track) != PLACEMENT_FIELDS or track[0] not in READ_KINDS:
        raise BuildFailure("readback-mismatch", f"{what}: bad track readback {track!r}")
    index = track[1]
    if isinstance(index, bool) or not isinstance(index, int) or index < 1:
        raise BuildFailure("readback-mismatch", f"{what}: bad track index {index!r}")
    prop = item.GetMediaPoolItem().GetClipProperty("File Path")
    if not isinstance(prop, str) or not prop:
        raise BuildFailure("readback-mismatch", f"{what}: no File Path property")
    linked: list[str] = []
    for link in item.GetLinkedItems() or ():
        link_id = link.GetUniqueId()
        if not isinstance(link_id, str) or not link_id:
            raise BuildFailure("readback-mismatch", f"{what}: linked item without unique id")
        linked.append(link_id)
    path = os.path.realpath(prop)
    return ReadbackRow(
        unique_id=uid,
        kind=track[0],
        track_index=index,
        record_start=record_start,
        record_end=record_end,
        source_start=source_start,
        source_duration=duration,
        media_path=path,
        media_sha256=sha256_of(Path(path)),
        linked_ids=tuple(sorted(linked)),
    )


__all__ = [
    "READ_KINDS",
    "ReadbackRow",
    "Sha256Of",
    "TimelineReadback",
    "as_frame",
    "capture_readback",
]
