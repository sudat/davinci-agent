"""Protocols and value models for the Phase-0A base-cut spike adapter.

The protocols mirror the official Resolve scripting surface this spike uses
(MediaPool import/append, timeline tracks, timeline-item readback). Value
models are the requested plan and the observed readback snapshot compared
against the frozen fixture manifest.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from services.contracts.primitives import SourceId, StrictModel, TrackKind


class BaseCutMediaPoolItemApi(Protocol):
    def GetClipProperty(self, property_name: str) -> str | dict[str, str]: ...


class BaseCutTimelineItemApi(Protocol):
    def GetUniqueId(self) -> str: ...

    def GetStart(self, subframe_precision: bool) -> int | float: ...

    def GetEnd(self, subframe_precision: bool) -> int | float: ...

    def GetDuration(self, subframe_precision: bool) -> int | float: ...

    def GetSourceStartFrame(self) -> int | float: ...

    def GetTrackTypeAndIndex(self) -> list[str | int]: ...

    def GetLinkedItems(self) -> list[BaseCutTimelineItemApi]: ...

    def GetMediaPoolItem(self) -> BaseCutMediaPoolItemApi: ...


class BaseCutTimelineApi(Protocol):
    def GetName(self) -> str: ...

    def GetTrackCount(self, track_type: str) -> int: ...

    def AddTrack(self, track_type: str) -> bool: ...

    def DeleteTrack(self, track_type: str, track_index: int) -> bool: ...

    def GetItemListInTrack(self, track_type: str, index: int) -> list[BaseCutTimelineItemApi]: ...

    def SetClipsLinked(self, items: list[BaseCutTimelineItemApi], linked: bool) -> bool: ...

    def GetStartFrame(self) -> int: ...

    def GetEndFrame(self) -> int: ...


class BaseCutMediaPoolApi(Protocol):
    def CreateEmptyTimeline(self, name: str) -> object: ...

    def ImportMedia(self, paths: list[str]) -> list[BaseCutMediaPoolItemApi]: ...

    def AppendToTimeline(
        self, clip_infos: list[dict[str, object]]
    ) -> list[BaseCutTimelineItemApi]: ...


class BaseCutProjectApi(Protocol):
    def GetSetting(self, setting_name: str) -> str: ...

    def SetSetting(self, setting_name: str, setting_value: str) -> bool: ...


class BaseCutItem(StrictModel):
    item_id: str
    kind: TrackKind
    source_id: SourceId
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=0)
    record_start: int = Field(ge=0)
    record_end: int = Field(ge=0)
    track_index: int = Field(gt=0)
    av_link_id: str


class BaseCutRequest(StrictModel):
    rate_num: int = Field(gt=0)
    rate_den: int = Field(gt=0)
    items: tuple[BaseCutItem, ...]
    media: dict[str, str]


class ReadItem(StrictModel):
    kind: TrackKind
    track_index: int
    record_start: int
    record_end: int
    source_start: int
    source_end: int
    media_path: str
    unique_id: str
    linked_ids: frozenset[str]


class TimelineSnapshot(StrictModel):
    video_track_count: int
    audio_track_count: int
    subtitle_track_count: int
    record_frame_count: int
    items: tuple[ReadItem, ...]
