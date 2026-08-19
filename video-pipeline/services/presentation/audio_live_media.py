"""Media/QC helpers for the Todo-59 live audio driver (owned project only).

The deterministic request builders, the pinned-ffmpeg measurement of a
RENDERED output (mono decode + the Todo-34 stack + ffprobe channel count),
and the Todo-52 audio-check policy view derived from the section targets.
The live media surface is the established, live-verified one: ImportMedia,
AppendToTimeline clipInfo records, structural readback, and the official
render job API under the frozen CompletionPercentage==100 rule.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

from services.qc.checks.audio_checks import (
    AudioMeasure,
    evaluate_audio_measurements,
    measure_audio,
)
from services.toolchain.render_qc import render_complete

if TYPE_CHECKING:
    from services.qc.models import AudioThresholds, QcIssue

MARKER: Final = "audio-live:"
FRAME_ORIGIN: Final = 108000
TIMELINE_START_TC: Final = "01:00:00:00"
RATE_NUM: Final = 30
BASE_SPAN: Final[tuple[int, int]] = (0, 600)
REPORT_NAME: Final = "audio-live-report.json"
RENDER_POLL_SECONDS: Final = 3.0
THRESHOLD_VERSION: Final = "audio-live-targets-v1"


class AudioLiveError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class AudioPoolItemApi(Protocol):
    def GetClipProperty(self, prop: str) -> object: ...


class LiveAudioTimelineApi(Protocol):
    def SetStartTimecode(self, timecode: str) -> bool: ...


class AudioTimelineItemApi(Protocol):
    def GetStart(self, subframe_precision: bool) -> int | float: ...

    def GetEnd(self, subframe_precision: bool) -> int | float: ...

    def GetTrackTypeAndIndex(self) -> list[str | int]: ...


class AudioPoolApi(Protocol):
    def ImportMedia(self, paths: list[str]) -> Sequence[object]: ...

    def AppendToTimeline(
        self, clip_infos: list[dict[str, object]]
    ) -> Sequence[object]: ...


class AudioRenderProjectApi(Protocol):
    def SetCurrentRenderFormatAndCodec(self, video_format: str, codec: str) -> bool: ...

    def SetRenderSettings(self, settings: dict[str, object]) -> bool: ...

    def AddRenderJob(self) -> str: ...

    def StartRendering(self, job_id: str) -> bool: ...

    def StopRendering(self) -> None: ...

    def GetRenderJobStatus(self, job_id: str) -> dict[str, object]: ...

    def GetRenderJobList(self) -> list[dict[str, object]]: ...


@dataclass(frozen=True, slots=True)
class LiveAudioPolicy:
    """Structural Todo-52 policy view over the section targets."""

    threshold_version: str
    audio: AudioThresholds


def import_media(pool: AudioPoolApi, paths: list[str]) -> dict[str, AudioPoolItemApi]:
    imported = pool.ImportMedia(sorted(set(paths)))
    if imported is None or len(imported) != len(set(paths)):
        raise AudioLiveError(
            "import_failed",
            f"ImportMedia returned {len(imported or [])} for {len(set(paths))} files",
        )
    by_real: dict[str, AudioPoolItemApi] = {}
    for item in imported:
        pool_item = cast("AudioPoolItemApi", item)
        real = os.path.realpath(str(pool_item.GetClipProperty("File Path")))
        by_real[real] = pool_item
    media: dict[str, AudioPoolItemApi] = {}
    for path in paths:
        found = by_real.get(os.path.realpath(path))
        if found is None:
            raise AudioLiveError("import_failed", f"imported media does not cover {path}")
        media[path] = found
    return media


def append_clip(pool: AudioPoolApi, info: dict[str, object]) -> AudioTimelineItemApi:
    added = pool.AppendToTimeline([info])
    if added is None or len(added) != 1:
        raise AudioLiveError("append_failed", "AppendToTimeline returned no item")
    return cast("AudioTimelineItemApi", added[0])


def verify_audio_readback(
    item: AudioTimelineItemApi,
    *,
    track_index: int,
    start_frame: int,
    end_frame: int,
) -> None:
    placement = item.GetTrackTypeAndIndex()
    observed_kind = str(placement[0]) if placement else "unknown"
    observed_index = int(placement[1]) if len(placement) > 1 else -1
    observed_start = int(item.GetStart(False))
    observed_end = int(item.GetEnd(False))
    if (
        observed_kind != "audio"
        or observed_index != track_index
        or observed_start != start_frame
        or observed_end != end_frame
    ):
        raise AudioLiveError(
            "wrong_placement",
            f"expected audio/{track_index} at {start_frame}..{end_frame}, observed "
            f"{observed_kind}/{observed_index} at {observed_start}..{observed_end}",
        )


def render_project(
    project: AudioRenderProjectApi, render_dir: Path, deadline_seconds: float
) -> Path:
    render_dir.mkdir(parents=True, exist_ok=True)
    if not project.SetCurrentRenderFormatAndCodec("MP4", "H264"):
        raise AudioLiveError("render_failed", "SetCurrentRenderFormatAndCodec failed")
    settings: dict[str, object] = {
        "TargetDir": str(render_dir),
        "CustomName": "fvp-audio-live",
        "FormatWidth": 1920,
        "FormatHeight": 1080,
        "FrameRate": RATE_NUM,
        "AudioCodec": "aac",
        "AudioSampleRate": 48000,
        "SelectAllFrames": True,
    }
    if not project.SetRenderSettings(settings):
        raise AudioLiveError("render_failed", "SetRenderSettings failed")
    job_id = project.AddRenderJob()
    if not isinstance(job_id, str) or not job_id:
        raise AudioLiveError("render_failed", "AddRenderJob returned no job id")
    entry = next(
        (row for row in project.GetRenderJobList() if row.get("JobId") == job_id),
        None,
    )
    if entry is None:
        raise AudioLiveError("render_failed", f"render job {job_id} missing from list")
    if not project.StartRendering(job_id):
        raise AudioLiveError("render_failed", f"StartRendering failed for {job_id}")
    deadline = time.monotonic() + deadline_seconds
    completed = False
    while time.monotonic() < deadline:
        if render_complete(project.GetRenderJobStatus(job_id)):
            completed = True
            break
        time.sleep(RENDER_POLL_SECONDS)
    if not completed:
        project.StopRendering()
        raise AudioLiveError(
            "render_failed", "render did not reach CompletionPercentage==100 in time"
        )
    output = Path(str(entry["TargetDir"])) / str(entry["OutputFilename"])
    if not output.is_file():
        raise AudioLiveError("render_failed", f"completed render has no output: {output}")
    return output


def _probed_channels(ffprobe_bin: Path, render: Path) -> int:
    argv = (
        str(ffprobe_bin),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(render),
    )
    result = subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise AudioLiveError("qc_failed", f"ffprobe failed: {result.stderr[-300:]}")
    payload: object = json.loads(result.stdout)
    streams = payload["streams"] if isinstance(payload, dict) else []
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "audio":
            channels = stream.get("channels")
            return int(channels) if isinstance(channels, int) else 0
    return 0


def qc_rendered_audio(
    ffmpeg_bin: Path,
    ffprobe_bin: Path,
    render: Path,
    thresholds: AudioThresholds,
    input_sha: str,
) -> tuple[AudioMeasure, tuple[QcIssue, ...]]:
    """Measure the RENDERED output with the Todo-34/52 stack; typed on issues."""

    def _mono_wav(source: Path, out_wav: Path) -> None:
        argv = (
            str(ffmpeg_bin),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        )
        decoded = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=300
        )
        if decoded.returncode != 0:
            raise AudioLiveError(
                "qc_failed", f"mono decode failed: {decoded.stderr[-300:]}"
            )

    with tempfile.TemporaryDirectory(prefix="audio-live-qc-") as tmp:
        tmp_wav = Path(tmp) / "measure.wav"
        measured = measure_audio(ffmpeg_bin, render, _mono_wav, tmp_wav)
    channels = _probed_channels(ffprobe_bin, render)
    measure = AudioMeasure(
        peak_sample=measured.peak_sample,
        peak_mb=measured.peak_mb,
        clipped_samples=measured.clipped_samples,
        loudness=measured.loudness,
        silence_spans=measured.silence_spans,
        probed_channels=channels,
    )
    policy = LiveAudioPolicy(threshold_version=THRESHOLD_VERSION, audio=thresholds)
    issues = evaluate_audio_measurements(measure, policy, (input_sha,))
    return measure, issues


__all__ = [
    "AudioLiveError",
    "AudioPoolApi",
    "AudioPoolItemApi",
    "AudioRenderProjectApi",
    "AudioTimelineItemApi",
    "LiveAudioPolicy",
    "LiveAudioTimelineApi",
    "append_clip",
    "import_media",
    "qc_rendered_audio",
    "render_project",
    "verify_audio_readback",
]
