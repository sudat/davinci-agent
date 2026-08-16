"""In-memory fakes for the Resolve public-API surface used by the base-cut spike.

``FakeBaseCutTimeline``/``FakeBaseCutMediaPool`` implement the documented
ImportMedia/AppendToTimeline/AddTrack/SetClipsLinked semantics so the real
build/readback path can run offline with an injected fault.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from services.resolve_bridge.base_cut_plan import BaseCutError

if TYPE_CHECKING:
    from services.resolve_bridge.base_cut_models import (
        BaseCutMediaPoolItemApi,
        BaseCutTimelineItemApi,
    )
    from services.resolve_bridge.lifecycle import TimelineApi

CUT_FIRST_RECORD_START = 30
CUT_SECOND_RECORD_START = 330
WRONG_TRACK_INDEX = 2


class FakeBaseCutPoolItem:
    def __init__(self, path: str) -> None:
        self._path = path

    def GetClipProperty(self, property_name: str) -> str | dict[str, str]:
        return self._path if property_name == "File Path" else ""


@dataclass
class FakeBaseCutItem:
    kind: str
    track_index: int
    record_start: int
    record_end: int
    source_start: int
    source_end: int
    pool_item: FakeBaseCutPoolItem
    unique_id: str
    registry: dict[str, BaseCutTimelineItemApi]
    links: dict[str, set[str]]
    placement: tuple[str, int] = field(init=False)

    def __post_init__(self) -> None:
        self.placement = (self.kind, self.track_index)

    def GetUniqueId(self) -> str:
        return self.unique_id

    def GetStart(self, subframe_precision: bool) -> int:
        return self.record_start

    def GetEnd(self, subframe_precision: bool) -> int:
        return self.record_end

    def GetDuration(self, subframe_precision: bool) -> int:
        return self.record_end - self.record_start

    def GetSourceStartFrame(self) -> int:
        return self.source_start

    def GetSourceEndFrame(self) -> int:
        return self.source_end

    def GetTrackTypeAndIndex(self) -> list[str | int]:
        return [self.kind, self.track_index]

    def GetLinkedItems(self) -> list[BaseCutTimelineItemApi]:
        return [self.registry[uid] for uid in sorted(self.links.get(self.unique_id, set()))]

    def GetMediaPoolItem(self) -> FakeBaseCutPoolItem:
        return self.pool_item


class FakeBaseCutTimeline:
    def __init__(self, name: str) -> None:
        self._name = name
        self._counts = {"video": 0, "audio": 0, "subtitle": 0}
        self._items: list[FakeBaseCutItem] = []
        self._registry: dict[str, BaseCutTimelineItemApi] = {}
        self._links: dict[str, set[str]] = {}

    def GetName(self) -> str:
        return self._name

    def GetTrackCount(self, track_type: str) -> int:
        return self._counts.get(track_type, 0)

    def AddTrack(self, track_type: str) -> bool:
        self._counts[track_type] = self._counts.get(track_type, 0) + 1
        return True

    def DeleteTrack(self, track_type: str, track_index: int) -> bool:
        occupied = any(item.placement == (track_type, track_index) for item in self._items)
        if occupied:
            return False
        self._counts[track_type] -= 1
        return True

    def GetItemListInTrack(self, track_type: str, index: int) -> list[BaseCutTimelineItemApi]:
        return [
            cast("BaseCutTimelineItemApi", item)
            for item in self._items
            if item.placement == (track_type, index)
        ]

    def SetClipsLinked(self, items: list[BaseCutTimelineItemApi], linked: bool) -> bool:
        uids = [item.GetUniqueId() for item in items]
        if linked:
            members = set(uids)
            for uid in uids:
                members |= self._links.get(uid, set())
            for uid in members:
                self._links[uid] = self._links.get(uid, set()) | {
                    other for other in members if other != uid
                }
        else:
            for uid in uids:
                self._links[uid] = self._links.get(uid, set()) - set(uids)
        return True

    def GetStartFrame(self) -> int:
        return min((item.GetStart(False) for item in self._items), default=0)

    def GetEndFrame(self) -> int:
        return max((item.GetEnd(False) for item in self._items), default=0)

    def append(
        self, kind: str, track_index: int, record_start: int, record_end: int,
        source_start: int, source_end: int, pool_item: FakeBaseCutPoolItem,
    ) -> BaseCutTimelineItemApi:
        unique_id = f"fake-{len(self._items) + 1:03d}"
        item = FakeBaseCutItem(
            kind,
            track_index,
            record_start,
            record_end,
            source_start,
            source_end,
            pool_item,
            unique_id,
            self._registry,
            self._links,
        )
        self._items.append(item)
        self._registry[unique_id] = item
        return item

    def auto_extend(self, track_type: str, track_index: int) -> None:
        while self._counts.get(track_type, 0) < track_index:
            self.AddTrack(track_type)


class FakeBaseCutMediaPool:
    def __init__(self, fault: str) -> None:
        self._fault = fault
        self._pool_items: dict[str, FakeBaseCutPoolItem] = {}
        self._timeline: FakeBaseCutTimeline | None = None

    def CreateEmptyTimeline(self, name: str) -> TimelineApi | None:
        timeline = FakeBaseCutTimeline(name)
        self._timeline = timeline
        return timeline

    def current_timeline(self) -> FakeBaseCutTimeline | None:
        return self._timeline

    def ImportMedia(self, paths: list[str]) -> list[BaseCutMediaPoolItemApi]:
        return [cast("BaseCutMediaPoolItemApi", self._pool_item(path)) for path in paths]

    def AppendToTimeline(self, clip_infos: list[dict[str, object]]) -> list[BaseCutTimelineItemApi]:
        timeline = self._timeline
        if timeline is None:
            raise BaseCutError("AppendToTimeline without a current timeline")
        added = [self._append_one(timeline, info) for info in clip_infos]
        if self._duplicates(clip_infos):
            for info in clip_infos:
                self._append_one(timeline, info)
        return added

    def _duplicates(self, clip_infos: list[dict[str, object]]) -> bool:
        if self._fault != "duplicate_append" or not clip_infos:
            return False
        return clip_infos[0].get("recordFrame") == CUT_FIRST_RECORD_START

    def _pool_item(self, path: str) -> FakeBaseCutPoolItem:
        if path not in self._pool_items:
            self._pool_items[path] = FakeBaseCutPoolItem(path)
        return self._pool_items[path]

    def _append_one(
        self, timeline: FakeBaseCutTimeline, info: dict[str, object]
    ) -> BaseCutTimelineItemApi:
        kind = "video" if info.get("mediaType") == 1 else "audio"
        track_index = self._resolve_track(timeline, kind, info)
        record_start = _field(info, "recordFrame")
        record_end = record_start + (_field(info, "endFrame") - _field(info, "startFrame"))
        if (
            self._fault == "off_by_one_frame"
            and kind == "video"
            and record_start == CUT_FIRST_RECORD_START
        ):
            record_end += 1
        return timeline.append(
            kind,
            track_index,
            record_start,
            record_end,
            _field(info, "startFrame"),
            _field(info, "endFrame"),
            self._resolve_media(info),
        )

    def _resolve_media(self, info: dict[str, object]) -> FakeBaseCutPoolItem:
        raw = info.get("mediaPoolItem")
        if not isinstance(raw, FakeBaseCutPoolItem):
            raise BaseCutError("clipInfo mediaPoolItem is not a fake pool item")
        path = raw.GetClipProperty("File Path")
        if self._fault == "wrong_media_same_duration" and isinstance(path, str) and path.endswith(
            "intro.mov"
        ):
            return self._pool_item(path.replace("intro.mov", "outro.mov"))
        return raw

    def _resolve_track(
        self, timeline: FakeBaseCutTimeline, kind: str, info: dict[str, object]
    ) -> int:
        track_index = _field(info, "trackIndex")
        if (
            self._fault == "wrong_track"
            and kind == "audio"
            and info.get("recordFrame") == CUT_SECOND_RECORD_START
        ):
            track_index = WRONG_TRACK_INDEX
        timeline.auto_extend(kind, track_index)
        return track_index


def _field(info: dict[str, object], key: str) -> int:
    value = info.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BaseCutError(f"clipInfo field {key} is not an integer: {value!r}")
    return value
