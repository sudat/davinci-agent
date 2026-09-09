"""Media operations for the Todo-58 live overlay driver (owned project).

Deterministic request builders plus the established, live-verified media
surface only: ImportMedia, AppendToTimeline clipInfo records, structural
readback, and the official render job API under the frozen
CompletionPercentage==100 rule.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Final, Protocol

from services.contracts.primitives import RecordFrameSpan
from services.outputs.geometry import scale_px_for_output
from services.presentation.overlay_models import (
    OverlayControlRequest,
    OverlayControlValue,
    OverlayGeometry,
    OverlayItemRequest,
    OverlayPlacementReadback,
)
from services.toolchain.render_qc import render_complete

GEO: Final = OverlayGeometry(
    timeline_width=1920,
    timeline_height=1080,
    safe_margin_px=96,
    region_width_px=480,
    region_height_px=270,
)


def geo_for(output_id: str) -> OverlayGeometry:
    """Overlay geometry for one enumerated output (landscape default).

    The vertical canvas is narrower and taller: the safe margin scales by
    canvas width while the region keeps its 16:9 shape scaled to fit.
    """
    if output_id != "vertical":
        return GEO
    return OverlayGeometry(
        timeline_width=1080,
        timeline_height=1920,
        safe_margin_px=scale_px_for_output(96, "vertical"),
        region_width_px=scale_px_for_output(480, "vertical"),
        region_height_px=scale_px_for_output(270, "vertical"),
    )


MARKER: Final = "overlay-live:"
FRAME_ORIGIN: Final = 108000
TIMELINE_START_TC: Final = "01:00:00:00"
RATE_NUM: Final = 30
BASE_SPAN: Final[tuple[int, int]] = (0, 600)
OVERLAY_SPAN: Final[tuple[int, int]] = (150, 240)
REPORT_NAME: Final = "overlay-live-report.json"
RENDER_POLL_SECONDS: Final = 3.0
OVERLAY_TRACKS: Final = 2


class OverlayLiveError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class LivePoolApi(Protocol):
    def ImportMedia(self, paths: list[str]) -> list[Any]: ...

    def AppendToTimeline(self, clip_infos: list[dict[str, object]]) -> list[Any]: ...


class LiveTimelineApi(Protocol):
    def GetName(self) -> str: ...

    def GetTrackCount(self, track_type: str) -> int: ...

    def AddTrack(self, track_type: str, sub_track_type: str = ...) -> bool: ...

    def SetStartTimecode(self, timecode: str) -> bool: ...

    def InsertFusionTitleIntoTimeline(self, title_name: str) -> object | None: ...


def _span(bounds: tuple[int, int]) -> Any:
    return RecordFrameSpan(start_frame=bounds[0], end_frame=bounds[1])


def _requests(probe_sha: str, overlay_sha: str, logo_sha: str) -> tuple[OverlayItemRequest, ...]:
    return (
        OverlayItemRequest(
            item_id="overlay.keyword-editing",
            role="keyword_overlay",
            text="keyword overlay fixture",
            record_span=_span(OVERLAY_SPAN),
            anchor="top-right",
            asset_path="assets/p3-brand-a/overlay.mov",
            asset_sha256=overlay_sha,
            declared_path="external_media",
        ),
        OverlayItemRequest(
            item_id="title.chapter-1",
            role="chapter_title",
            text="Chapter One",
            record_span=_span(OVERLAY_SPAN),
            anchor="bottom-left",
            asset_path="assets/p3-brand-a/logo.mov",
            asset_sha256=logo_sha,
            declared_path="fusion_template",
            font_family="Open Sans",
            font_evidence_sha256=probe_sha,
            fusion_controls=(
                OverlayControlRequest(
                    control_id="StyledText",
                    value=OverlayControlValue(text="Chapter One"),
                ),
                OverlayControlRequest(
                    control_id="Size", value=OverlayControlValue(number=0.083)
                ),
                OverlayControlRequest(
                    control_id="Center", value=OverlayControlValue(point=(0.1, 0.9))
                ),
            ),
        ),
    )


def _ensure_tracks(timeline: LiveTimelineApi) -> None:
    while timeline.GetTrackCount("video") < OVERLAY_TRACKS:
        if not timeline.AddTrack("video"):
            raise OverlayLiveError("AddTrack video failed")
    while timeline.GetTrackCount("audio") < 1:
        if not timeline.AddTrack("audio", "stereo"):
            raise OverlayLiveError('AddTrack audio "stereo" failed')


def _import(pool: LivePoolApi, paths: list[str]) -> dict[str, Any]:
    imported = pool.ImportMedia(sorted(set(paths)))
    if imported is None or len(imported) != len(set(paths)):
        raise OverlayLiveError(
            f"ImportMedia returned {len(imported or [])} for {len(set(paths))} files"
        )
    by_real: dict[str, Any] = {}
    for item in imported:
        prop = item.GetClipProperty("File Path")
        by_real[os.path.realpath(str(prop))] = item
    media: dict[str, Any] = {}
    for path in paths:
        media[path] = by_real.get(os.path.realpath(path))
        if media[path] is None:
            raise OverlayLiveError(f"imported media does not cover {path}")
    return media


def _append(pool: LivePoolApi, info: dict[str, object]) -> Any:
    added = pool.AppendToTimeline([info])
    if added is None or len(added) != 1:
        raise OverlayLiveError("AppendToTimeline returned no item")
    return added[0]


def _readback(item_id: str, item: Any) -> OverlayPlacementReadback:
    placement = item.GetTrackTypeAndIndex()
    return OverlayPlacementReadback(
        item_id=item_id,
        track_type=str(placement[0]),
        track_index=int(placement[1]),
        record_start=int(item.GetStart(False)),
        record_end=int(item.GetEnd(False)),
    )


def _render_project(project: Any, render_dir: Path, deadline_seconds: float) -> Path:
    render_dir.mkdir(parents=True, exist_ok=True)
    if not project.SetCurrentRenderFormatAndCodec("MP4", "H264"):
        raise OverlayLiveError('SetCurrentRenderFormatAndCodec("MP4", "H264") failed')
    settings: dict[str, object] = {
        "TargetDir": str(render_dir),
        "CustomName": "fvp-overlay-live",
        "FormatWidth": GEO.timeline_width,
        "FormatHeight": GEO.timeline_height,
        "FrameRate": RATE_NUM,
        "AudioCodec": "aac",
        "AudioSampleRate": 48000,
        "SelectAllFrames": True,
    }
    if not project.SetRenderSettings(settings):
        raise OverlayLiveError("SetRenderSettings failed")
    job_id = project.AddRenderJob()
    if not isinstance(job_id, str) or not job_id:
        raise OverlayLiveError("AddRenderJob returned no job id")
    entry = next(
        (row for row in project.GetRenderJobList() if row.get("JobId") == job_id), None
    )
    if entry is None:
        raise OverlayLiveError(f"render job {job_id} missing from GetRenderJobList")
    if not project.StartRendering(job_id):
        raise OverlayLiveError(f"StartRendering failed for {job_id}")
    deadline = time.monotonic() + deadline_seconds
    completed = False
    while time.monotonic() < deadline:
        if render_complete(project.GetRenderJobStatus(job_id)):
            completed = True
            break
        time.sleep(RENDER_POLL_SECONDS)
    if not completed:
        project.StopRendering()
        raise OverlayLiveError("render did not reach CompletionPercentage==100 in time")
    output = Path(str(entry["TargetDir"])) / str(entry["OutputFilename"])
    if not output.is_file():
        raise OverlayLiveError(f"completed render has no output: {output}")
    return output


