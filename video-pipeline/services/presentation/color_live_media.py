"""LIVE color media helpers (Todo 60): the declared-recipe bars fixture.

The bars source is generated ONCE per bundle from the declared pinned
recipe (``smptebars`` + bt709 ``setparams`` + ``h264_videotoolbox`` with
declared color tags AND declared camera container tags), so the source's
own ffprobe payload carries the DECLARED colorimetry and camera identity
the selection runs on — nothing about the camera is invented by the flow.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Final, Protocol, cast

from services.presentation.color_models import DeclaredCameraColor
from services.toolchain.render_qc import render_complete

MARKER: Final = "color-live:"
FFMPEG_TIMEOUT_SECONDS: Final = 300
PROBE_TIMEOUT_SECONDS: Final = 120
RENDER_POLL_SECONDS: Final = 3.0
FRAME_ORIGIN: Final = 108000
FIXTURE_MAKE: Final = "FIXTURECAM"
FIXTURE_MODEL: Final = "FC-1"
TIMELINE_START_TC: Final = "01:00:00:00"


class ColorLiveError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class ColorPoolApi(Protocol):
    def ImportMedia(self, paths: list[str]) -> list[object] | None: ...

    def AppendToTimeline(
        self, clip_infos: list[dict[str, object]]
    ) -> list[object] | None: ...


class ColorTimelineItemApi(Protocol):
    def GetStart(self, flag: bool) -> int: ...

    def GetEnd(self, flag: bool) -> int: ...

    def GetTrackTypeAndIndex(self) -> list[str | int]: ...


class ColorTimelineApi(Protocol):
    def SetStartTimecode(self, timecode: str) -> bool: ...


class ColorRenderProjectApi(Protocol):
    def SetCurrentRenderFormatAndCodec(
        self, format_name: str, codec: str
    ) -> bool: ...

    def SetRenderSettings(self, settings: dict[str, object]) -> bool: ...

    def AddRenderJob(self) -> object: ...

    def GetRenderJobList(self) -> list[dict[str, object]]: ...

    def GetRenderJobStatus(self, job_id: str) -> dict[str, object]: ...

    def StartRendering(self, job_id: str) -> bool: ...

    def StopRendering(self) -> bool: ...


def render_bars_source(
    *,
    ffmpeg_bin: Path,
    width: int,
    height: int,
    rate_num: int,
    seconds: int,
    output: Path,
) -> Path:
    """Generate the declared-recipe bars fixture (deterministic argv)."""

    argv = (
        str(ffmpeg_bin),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"smptebars=size={width}x{height}:rate={rate_num}",
        "-t",
        str(seconds),
        "-vf",
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
        "-c:v",
        "h264_videotoolbox",
        "-pix_fmt",
        "yuv420p",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-metadata",
        f"make={FIXTURE_MAKE}",
        "-metadata",
        f"model={FIXTURE_MODEL}",
        str(output),
    )
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as error:
        raise ColorLiveError(
            "bars_timeout", f"bars render exceeded {FFMPEG_TIMEOUT_SECONDS}s"
        ) from error
    if result.returncode != 0 or not output.is_file():
        raise ColorLiveError("bars_failed", result.stderr.strip()[-500:])
    return output


def _ffprobe_payload(ffprobe_bin: Path, media: Path) -> dict[str, object]:
    result = subprocess.run(
        (
            str(ffprobe_bin),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ColorLiveError("probe_failed", result.stderr.strip()[-300:])
    payload: object = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ColorLiveError("probe_failed", f"malformed ffprobe payload for {media}")
    return payload


def declared_color_from_media(
    ffprobe_bin: Path, media: Path, *, source_id: str
) -> DeclaredCameraColor:
    """The DECLARED color/camera metadata read from the media's own payload."""

    payload = _ffprobe_payload(ffprobe_bin, media)
    streams = payload.get("streams")
    video = next(
        (
            row
            for row in (streams if isinstance(streams, list) else [])
            if isinstance(row, dict) and row.get("codec_type") == "video"
        ),
        None,
    )
    if video is None:
        raise ColorLiveError("probe_failed", f"no video stream in {media}")
    tags = payload.get("format")
    tag_map = tags.get("tags") if isinstance(tags, dict) else None
    make = tag_map.get("make") if isinstance(tag_map, dict) else None
    model = tag_map.get("model") if isinstance(tag_map, dict) else None
    missing = [
        str(name)
        for name in ("color_space", "color_transfer", "color_primaries")
        if not video.get(name)
    ]
    if missing:
        raise ColorLiveError(
            "missing_color_metadata",
            f"{media} declares no {', '.join(missing)}",
        )
    return DeclaredCameraColor(
        source_id=source_id,
        camera_make=str(make) if make else None,
        camera_model=str(model) if model else None,
        color_space=str(video["color_space"]),
        color_transfer=str(video["color_transfer"]),
        color_primaries=str(video["color_primaries"]),
        color_range=str(video["color_range"]) if video.get("color_range") else None,
    )


def append_video_clip(
    pool: ColorPoolApi, media_path: Path, *, start_frame: int, end_frame: int
) -> ColorTimelineItemApi:
    media = pool.ImportMedia([str(media_path)])
    if media is None or len(media) != 1:
        raise ColorLiveError("import_failed", f"ImportMedia failed for {media_path}")
    added = pool.AppendToTimeline(
        [
            {
                "mediaPoolItem": media[0],
                "startFrame": start_frame,
                "endFrame": end_frame,
                "mediaType": 1,
                "trackIndex": 1,
                "recordFrame": FRAME_ORIGIN,
            }
        ]
    )
    if added is None or len(added) != 1:
        raise ColorLiveError("append_failed", "AppendToTimeline returned no item")
    return cast("ColorTimelineItemApi", added[0])


def verify_clip_readback(
    item: ColorTimelineItemApi, *, start_frame: int, end_frame: int
) -> None:
    placement = item.GetTrackTypeAndIndex()
    observed_kind = str(placement[0]) if placement else "unknown"
    observed_start = int(item.GetStart(False))
    observed_end = int(item.GetEnd(False))
    if observed_kind != "video" or observed_start != FRAME_ORIGIN or observed_end != (
        FRAME_ORIGIN + end_frame - start_frame
    ):
        raise ColorLiveError(
            "wrong_placement",
            f"expected video at {FRAME_ORIGIN}..{FRAME_ORIGIN + end_frame - start_frame}, "
            f"observed {observed_kind} at {observed_start}..{observed_end}",
        )


def render_project(
    project: ColorRenderProjectApi, render_dir: Path, deadline_seconds: float
) -> Path:
    """Render through the official Deliver API; verified from output bytes."""

    render_dir.mkdir(parents=True, exist_ok=True)
    if not project.SetCurrentRenderFormatAndCodec("MP4", "H264"):
        raise ColorLiveError("render_failed", "SetCurrentRenderFormatAndCodec failed")
    settings: dict[str, object] = {
        "TargetDir": str(render_dir),
        "CustomName": "fvp-color-live",
        "FormatWidth": 1920,
        "FormatHeight": 1080,
        "FrameRate": 30,
        "VideoCodec": "h264_videotoolbox",
        "SelectAllFrames": True,
    }
    if not project.SetRenderSettings(settings):
        raise ColorLiveError("render_failed", "SetRenderSettings failed")
    job_id = project.AddRenderJob()
    if not isinstance(job_id, str) or not job_id:
        raise ColorLiveError("render_failed", "AddRenderJob returned no job id")
    entry = next(
        (row for row in project.GetRenderJobList() if row.get("JobId") == job_id),
        None,
    )
    if entry is None:
        raise ColorLiveError("render_failed", f"render job {job_id} missing from list")
    if not project.StartRendering(job_id):
        raise ColorLiveError("render_failed", f"StartRendering failed for {job_id}")
    deadline = time.monotonic() + deadline_seconds
    completed = False
    while time.monotonic() < deadline:
        if render_complete(project.GetRenderJobStatus(job_id)):
            completed = True
            break
        time.sleep(RENDER_POLL_SECONDS)
    if not completed:
        project.StopRendering()
        raise ColorLiveError(
            "render_failed", "render did not reach CompletionPercentage==100 in time"
        )
    output = Path(str(entry["TargetDir"])) / str(entry["OutputFilename"])
    if not output.is_file():
        raise ColorLiveError("render_failed", f"completed render has no output: {output}")
    return output


__all__ = [
    "FIXTURE_MAKE",
    "FIXTURE_MODEL",
    "FRAME_ORIGIN",
    "MARKER",
    "ColorLiveError",
    "ColorPoolApi",
    "ColorRenderProjectApi",
    "ColorTimelineApi",
    "ColorTimelineItemApi",
    "append_video_clip",
    "declared_color_from_media",
    "render_bars_source",
    "render_project",
    "verify_clip_readback",
]
