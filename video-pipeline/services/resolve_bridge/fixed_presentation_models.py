"""Protocols and value models for the Phase-0A fixed-presentation spike.

The protocols mirror the official Resolve scripting surface this spike uses on
top of the base-cut surface: timeline start timecode, explicit stereo audio and
subtitle tracks, the Deliver-page render job APIs, and subtitle media-pool
import. Value models carry the strategy ladder, readback evidence, ffprobe
summaries, and the final report compared against the frozen fixture manifest.

Live-verified coordinate caveat (Resolve 21.0.4): AppendToTimeline recordFrame,
TimelineItem GetStart/GetEnd, timeline marks, and render job MarkIn/MarkOut all
live in one timeline-absolute frame space whose origin is the timeline start
timecode (01:00:00:00 => frame 108000 at 30 fps). Placing clips at frames below
the origin silently hides them from the render engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol

from pydantic import Field

from services.contracts.primitives import StrictModel

MARKER: Final = "fixed-presentation:"
EXIT_UNAVAILABLE: Final = 4
TIMELINE_START_TC: Final = "01:00:00:00"
FRAME_ORIGIN: Final = 108000
SUBTITLE_SRT: Final = "subtitle.srt"
REPORT_NAME: Final = "fixed-presentation-report.json"
STRATEGY_NAME: Final = "strategy-table.json"
READBACK_NAME: Final = "readback-table.json"
FFPROBE_NAME: Final = "render-ffprobe.json"
SHA_NAME: Final = "render-sha256.txt"


@dataclass(frozen=True, slots=True)
class SpikeRunOutcome:
    """Raw consolidation of one fixed-presentation spike run.

    Carries the human-readable report plus the raw readback snapshot and
    comparison outcome the item-level Build-Report writer consumes.
    """

    report: FixedPresentationReport
    render_summary: str
    snapshot: TimelineSnapshot
    outcome: CompareOutcome


if TYPE_CHECKING:
    from services.resolve_bridge.base_cut_compare import CompareOutcome
    from services.resolve_bridge.base_cut_models import (
        BaseCutMediaPoolItemApi,
        BaseCutTimelineItemApi,
        TimelineSnapshot,
    )

StrategyRung = Literal["direct", "interchange", "template", "external"]
ElementStatus = Literal["verified", "partial", "unsupported"]
DurationSeconds = str
FrameRateString = str


class FixedTimelineApi(Protocol):
    def GetName(self) -> str: ...

    def GetTrackCount(self, track_type: str) -> int: ...

    def AddTrack(self, track_type: str, sub_track_type: str = ...) -> bool: ...

    def DeleteTrack(self, track_type: str, track_index: int) -> bool: ...

    def GetItemListInTrack(
        self, track_type: str, index: int
    ) -> list[BaseCutTimelineItemApi]: ...

    def SetClipsLinked(self, items: list[BaseCutTimelineItemApi], linked: bool) -> bool: ...

    def GetStartFrame(self) -> int: ...

    def GetEndFrame(self) -> int: ...

    def SetStartTimecode(self, timecode: str) -> bool: ...

    def GetStartTimecode(self) -> str: ...


class FixedMediaPoolApi(Protocol):
    def CreateEmptyTimeline(self, name: str) -> object: ...

    def ImportMedia(self, paths: list[str]) -> list[BaseCutMediaPoolItemApi]: ...

    def AppendToTimeline(
        self, clip_infos: list[dict[str, object]]
    ) -> list[BaseCutTimelineItemApi]: ...


class FixedProjectApi(Protocol):
    def GetSetting(self, setting_name: str) -> str: ...

    def SetSetting(self, setting_name: str, setting_value: str) -> bool: ...

    def GetMediaPool(self) -> FixedMediaPoolApi: ...

    def SetCurrentRenderFormatAndCodec(self, video_format: str, codec: str) -> bool: ...

    def SetRenderSettings(self, settings: dict[str, object]) -> bool: ...

    def AddRenderJob(self) -> str: ...

    def StartRendering(self, job_id: str) -> bool: ...

    def StopRendering(self) -> None: ...

    def GetRenderJobStatus(self, job_id: str) -> dict[str, object]: ...

    def GetRenderJobList(self) -> list[dict[str, object]]: ...


class MismatchSink(Protocol):
    def add(self, code: str, detail: str) -> None: ...


class RungAttempt(StrictModel):
    rung: StrategyRung
    available: bool
    reason: str


class ElementStrategy(StrictModel):
    element: str
    strategy: StrategyRung | None
    status: ElementStatus
    reason: str
    limitations: str
    ladder: tuple[RungAttempt, ...]


class SubtitleCue(StrictModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str

    @property
    def span_seconds(self) -> str:
        return f"{self.start_ms / 1000:.3f}-{self.end_ms / 1000:.3f}"


class SubtitleArtifactEvidence(StrictModel):
    srt_sha256: str
    cue: SubtitleCue
    cue_matches_table: bool
    paired_path: str
    paired_sha256: str
    subtitle_stream_codec: str | None
    demux_matches_srt: bool
    packet_pts_seconds: float | None
    packet_duration_seconds: float | None


class SubtitleEvidence(StrictModel):
    strategy: ElementStrategy
    in_timeline: bool
    artifact: SubtitleArtifactEvidence | None


class SlateEvidence(StrictModel):
    item_id: str
    media_path: str
    record_start: int
    record_end: int
    duration_frames: int
    link_partner_id: str | None
    link_ok: bool
    media_ok: bool
    span_ok: bool


class FfprobeStream(StrictModel):
    codec_type: str | None = None
    codec_name: str | None = None
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    r_frame_rate: FrameRateString | None = None
    avg_frame_rate: FrameRateString | None = None
    nb_frames: str | None = None
    sample_rate: str | None = None
    channels: int | None = None
    duration: DurationSeconds | None = None


class FfprobeFormat(StrictModel):
    format_name: str | None = None
    duration: DurationSeconds | None = None


class FfprobeReport(StrictModel):
    streams: tuple[FfprobeStream, ...]
    format: FfprobeFormat


class RenderEvidence(StrictModel):
    job_id: str
    status: str
    output_path: str
    output_sha256: str
    report: FfprobeReport
    marks_in: int | None
    marks_out: int | None
    job_created_at: str = ""
    job_started_at: str = ""
    job_completed_at: str = ""
    poll_count: int = 0
    completion_percentage: int = -1


class FixedPresentationMismatch(StrictModel):
    code: str
    detail: str


class FixedPresentationReport(StrictModel):
    schema_version: str
    fixture_id: str
    strategy_table: tuple[ElementStrategy, ...]
    base_cut_passed: bool
    slates: tuple[SlateEvidence, ...]
    subtitle: SubtitleEvidence
    render: RenderEvidence
    mismatches: tuple[FixedPresentationMismatch, ...]
    passed: bool
