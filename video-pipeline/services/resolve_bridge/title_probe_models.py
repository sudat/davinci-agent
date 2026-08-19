"""Protocols and report models for the Todo-58 title capability probe.

The protocols mirror ONLY the documented Resolve 21 scripting surface the
probe touches: ``Timeline.InsertFusionTitleIntoTimeline`` (Edit-page Fusion
title templates), ``TimelineItem`` structural readback plus Fusion
composition access, and the Fusion ``comp.FindToolByID`` / published-control
``GetInput``/``SetInput`` calls. The report records every step honestly —
verified / unsupported / failed / watchdog-timeout — and never upgrades an
unobserved step to verified.
"""

from __future__ import annotations

from typing import Literal, Protocol

from services.contracts.primitives import StrictModel

StepStatus = Literal["verified", "failed", "unsupported", "timeout"]
PlaceRung = Literal["insert_fusion_title", "media_pool_append", "none"]


class TitleTimelineApi(Protocol):
    def GetName(self) -> str: ...

    def GetTrackCount(self, track_type: str) -> int: ...

    def AddTrack(self, track_type: str, sub_track_type: str = ...) -> bool: ...

    def SetStartTimecode(self, timecode: str) -> bool: ...

    def InsertFusionTitleIntoTimeline(self, title_name: str) -> object | None: ...


class TitleItemApi(Protocol):
    def GetName(self) -> str: ...

    def GetStart(self, subframe_precision: bool) -> int | float: ...

    def GetEnd(self, subframe_precision: bool) -> int | float: ...

    def GetDuration(self, subframe_precision: bool) -> int | float: ...

    def GetTrackTypeAndIndex(self) -> list[str | int]: ...

    def GetFusionCompCount(self) -> int: ...

    def GetFusionCompByIndex(self, comp_index: int) -> object | None: ...


class FusionCompApi(Protocol):
    def FindToolByID(self, tool_id: str) -> object | None: ...


class FusionToolApi(Protocol):
    def GetInput(self, control: str) -> object: ...

    def SetInput(self, control: str, value: object) -> object: ...


class ProbeStep(StrictModel):
    step: str
    status: StepStatus
    detail: str = ""


class ControlProbe(StrictModel):
    control: str
    write_value_text: str
    read_before: str
    write_returned: str
    read_after: str
    readback_matches: bool


class PlacementReadback(StrictModel):
    record_start: int
    record_end: int
    duration: int
    track_type: str
    track_index: int


class TitleProbeReport(StrictModel):
    """The honest capability verdict for the Text+ (Fusion title) surface."""

    schema_version: Literal["title-probe-report-v1"]
    binding: str
    place_rung: PlaceRung
    placed: bool
    structural: PlacementReadback | None
    comp_accessed: bool
    tool_found: bool
    controls: tuple[ControlProbe, ...]
    steps: tuple[ProbeStep, ...]

    @property
    def fusion_supported(self) -> bool:
        controls_ok = bool(self.controls) and all(
            row.readback_matches for row in self.controls
        )
        return (
            self.placed
            and self.structural is not None
            and self.comp_accessed
            and self.tool_found
            and controls_ok
        )


class ChildOutcome(StrictModel):
    """What the watchdog parent observed about the guarded probe child."""

    watchdog: Literal["completed", "timeout", "crash", "unavailable"]
    detail: str = ""


__all__ = [
    "ChildOutcome",
    "ControlProbe",
    "FusionCompApi",
    "FusionToolApi",
    "PlacementReadback",
    "ProbeStep",
    "StepStatus",
    "TitleItemApi",
    "TitleProbeReport",
    "TitleTimelineApi",
]
