"""LIVE parity media helpers (Todo 61): parameterized renders + probes.

The established, live-verified render surface (ImportMedia /
AppendToTimeline readback / the official render job API under the frozen
``CompletionPercentage == 100`` rule) with ONE parameterization: the same
timeline renders once at preview resolution and once at final resolution.
Probes are measured from the rendered bytes only — ffprobe facts, the
Todo-34/52 audio measurement stack, and raw SMPTE-bar region luma means.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Final, Protocol

from services.presentation.color_render import BAR_REGION_LUMA, REGION_Y
from services.toolchain.render_qc import render_complete

MARKER: Final = "parity-live:"
RENDER_POLL_SECONDS: Final = 3.0
EXTRACT_TIMEOUT_SECONDS: Final = 120
REGION_INSET_FRACTION: Final = 4


class ParityLiveError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class ParityRenderProjectApi(Protocol):
    def SetCurrentRenderFormatAndCodec(self, video_format: str, codec: str) -> bool: ...

    def SetRenderSettings(self, settings: dict[str, object]) -> bool: ...

    def AddRenderJob(self) -> str: ...

    def GetRenderJobList(self) -> list[dict[str, object]]: ...

    def GetRenderJobStatus(self, job_id: str) -> dict[str, object]: ...

    def StartRendering(self, job_id: str) -> bool: ...

    def StopRendering(self) -> None: ...


def render_with_settings(
    project: ParityRenderProjectApi,
    render_dir: Path,
    *,
    width: int,
    height: int,
    custom_name: str,
    deadline_seconds: float,
) -> Path:
    """Render the CURRENT timeline at the declared geometry; bounded wait."""

    render_dir.mkdir(parents=True, exist_ok=True)
    if not project.SetCurrentRenderFormatAndCodec("MP4", "H264"):
        raise ParityLiveError("render_failed", "SetCurrentRenderFormatAndCodec failed")
    settings: dict[str, object] = {
        "TargetDir": str(render_dir),
        "CustomName": custom_name,
        "FormatWidth": width,
        "FormatHeight": height,
        "AudioCodec": "aac",
        "AudioSampleRate": 48000,
        "SelectAllFrames": True,
    }
    if not project.SetRenderSettings(settings):
        raise ParityLiveError("render_failed", "SetRenderSettings failed")
    job_id = project.AddRenderJob()
    if not isinstance(job_id, str) or not job_id:
        raise ParityLiveError("render_failed", "AddRenderJob returned no job id")
    entry = next(
        (row for row in project.GetRenderJobList() if row.get("JobId") == job_id),
        None,
    )
    if entry is None:
        raise ParityLiveError("render_failed", f"render job {job_id} missing from list")
    if not project.StartRendering(job_id):
        raise ParityLiveError("render_failed", f"StartRendering failed for {job_id}")
    deadline = time.monotonic() + deadline_seconds
    completed = False
    while time.monotonic() < deadline:
        if render_complete(project.GetRenderJobStatus(job_id)):
            completed = True
            break
        time.sleep(RENDER_POLL_SECONDS)
    if not completed:
        project.StopRendering()
        raise ParityLiveError(
            "render_failed", f"render {custom_name} did not complete in time"
        )
    output = Path(str(entry["TargetDir"])) / str(entry["OutputFilename"])
    if not output.is_file():
        raise ParityLiveError("render_failed", f"completed render missing: {output}")
    return output


def _probe_payload(ffprobe_bin: Path, media: Path) -> dict[str, object]:
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
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ParityLiveError("probe_failed", result.stderr.strip()[-300:])
    payload: object = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ParityLiveError("probe_failed", f"ffprobe payload malformed: {media}")
    return payload


def _video_stream(payload: dict[str, object]) -> dict[str, object]:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise ParityLiveError("probe_failed", "ffprobe payload carries no streams")
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            return stream
    raise ParityLiveError("probe_failed", "no video stream")


def probe_duration_ms(ffprobe_bin: Path, media: Path) -> int:
    """Container/stream duration in integer milliseconds (measured, not declared)."""

    payload = _probe_payload(ffprobe_bin, media)
    fmt = payload.get("format")
    candidates = [
        fmt.get("duration") if isinstance(fmt, dict) else None,
        _video_stream(payload).get("duration"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return int(float(value) * 1000)
    raise ParityLiveError("probe_failed", f"no duration reported for {media}")


def probe_color_metadata(ffprobe_bin: Path, media: Path) -> dict[str, str]:
    stream = _video_stream(_probe_payload(ffprobe_bin, media))
    return {
        "color_space": str(stream.get("color_space") or ""),
        "color_transfer": str(stream.get("color_transfer") or ""),
        "color_primaries": str(stream.get("color_primaries") or ""),
    }


def probe_bar_region_means(
    ffmpeg_bin: Path, media: Path, *, width: int, height: int, frame_index: int = 0
) -> dict[str, int]:
    """Raw top-bar luma means (Todo-60 layout), no constant validation."""

    argv = (
        str(ffmpeg_bin),
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(media),
        "-vf",
        f"select=eq(n\\,{frame_index})",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    )
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, timeout=EXTRACT_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as error:
        raise ParityLiveError("probe_failed", "frame extraction timed out") from error
    if result.returncode != 0 or len(result.stdout) != width * height:
        detail = f"frame {frame_index} extraction got {len(result.stdout)} bytes"
        raise ParityLiveError("probe_failed", f"{detail}: {result.stderr[-200:]!r}")
    frame = result.stdout
    bar_width = width // len(BAR_REGION_LUMA)
    y0, y1 = REGION_Y
    inset = max(1, bar_width // REGION_INSET_FRACTION)
    means: dict[str, int] = {}
    for index, name in enumerate(BAR_REGION_LUMA):
        x0 = index * bar_width + inset
        x1 = (index + 1) * bar_width - inset
        samples = [
            frame[y * width + x]
            for y in range(y0, y1, 2)
            for x in range(max(0, x0), min(width, x1), 2)
        ]
        if not samples:
            raise ParityLiveError("probe_failed", f"region {name} sampled no pixels")
        means[name] = sum(samples) // len(samples)
    return means


__all__ = [
    "MARKER",
    "ParityLiveError",
    "ParityRenderProjectApi",
    "probe_bar_region_means",
    "probe_color_metadata",
    "probe_duration_ms",
    "render_with_settings",
]
