"""Deliver-page render runner and frozen-expectation comparison for the 0A spike.

Drives the official render job API (SetCurrentRenderFormatAndCodec,
SetRenderSettings, AddRenderJob, StartRendering, bounded GetRenderJobStatus
polling) and compares the output probe against the frozen manifest render
expectations. Render job status strings are localized on this host, so
completion is detected via CompletionPercentage.

Live-verified durations (Resolve 21.0.4): the video stream duration equals the
record extent exactly (660 frames / 30 fps = 22.0 s), while the AAC encoder
pads the audio stream and the container reports the longer audio duration, so
the container duration is asserted to be the video duration bounded from above
by half a second rather than exact equality.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.resolve_bridge.fixed_presentation_models import (
    FixedPresentationMismatch,
    FixedProjectApi,
    RenderEvidence,
)
from services.resolve_bridge.fixed_presentation_tools import MediaToolsApi, RenderError

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest

RENDER_POLL_SECONDS: Final = 2.0
RENDER_DEADLINE_SECONDS: Final = 300.0
MAX_CONTAINER_PADDING_SECONDS: Final = Fraction(1, 2)
VIDEO_FORMAT_KEY: Final = "MP4"
VIDEO_CODEC_KEY: Final = "H264"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def render_timeline(
    project: FixedProjectApi,
    render_dir: Path,
    manifest: Phase0AFixtureManifest,
    tools: MediaToolsApi,
) -> RenderEvidence:
    preset = manifest.recipe.render_preset
    render_dir.mkdir(parents=True, exist_ok=True)
    format_codec = f"SetCurrentRenderFormatAndCodec({VIDEO_FORMAT_KEY},{VIDEO_CODEC_KEY})"
    if not project.SetCurrentRenderFormatAndCodec(VIDEO_FORMAT_KEY, VIDEO_CODEC_KEY):
        raise RenderError(f"{format_codec} failed")
    settings: dict[str, object] = {
        "TargetDir": str(render_dir),
        "CustomName": "fixed-presentation",
        "FormatWidth": 1920,
        "FormatHeight": 1080,
        "FrameRate": manifest.recipe.source.frame_rate.num,
        "AudioCodec": preset.audio_codec,
        "AudioSampleRate": preset.audio_sample_rate,
        "SelectAllFrames": True,
    }
    if not project.SetRenderSettings(settings):
        raise RenderError("SetRenderSettings failed")
    created_at = _utc_now()
    job_id = project.AddRenderJob()
    if not isinstance(job_id, str) or not job_id:
        raise RenderError("AddRenderJob returned no job id")
    entry = _job_entry(project, job_id)
    started_at = _utc_now()
    if not project.StartRendering(job_id):
        raise RenderError("StartRendering failed")
    deadline = time.monotonic() + RENDER_DEADLINE_SECONDS
    status = "incomplete"
    polls = 0
    percentage_seen = -1
    completed_at = ""
    while time.monotonic() < deadline:
        raw = project.GetRenderJobStatus(job_id)
        polls += 1
        percentage = raw.get("CompletionPercentage")
        if isinstance(percentage, int | float) and not isinstance(percentage, bool):
            percentage_seen = int(percentage)
        if isinstance(percentage, int | float) and percentage == 100:
            status = "complete"
            completed_at = _utc_now()
            break
        time.sleep(RENDER_POLL_SECONDS)
    if status != "complete":
        project.StopRendering()
        raise RenderError(f"render job did not complete within {RENDER_DEADLINE_SECONDS:.0f}s")
    output = Path(str(entry["TargetDir"])) / str(entry["OutputFilename"])
    if not output.is_file():
        raise RenderError(f"render job complete but output file missing: {output}")
    return RenderEvidence(
        job_id=job_id,
        status=status,
        output_path=str(output),
        output_sha256=tools.sha256(output),
        report=tools.probe(output),
        marks_in=_optional_int(entry.get("MarkIn")),
        marks_out=_optional_int(entry.get("MarkOut")),
        job_created_at=created_at,
        job_started_at=started_at,
        job_completed_at=completed_at,
        poll_count=polls,
        completion_percentage=percentage_seen,
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _job_entry(project: FixedProjectApi, job_id: str) -> dict[str, object]:
    for entry in project.GetRenderJobList():
        if entry.get("JobId") == job_id:
            return entry
    raise RenderError(f"render job {job_id} missing from GetRenderJobList")


def _fraction(text: str) -> Fraction:
    return Fraction(text)


def compare_render(
    evidence: RenderEvidence, manifest: Phase0AFixtureManifest
) -> tuple[str, tuple[FixedPresentationMismatch, ...]]:
    expected = manifest.expected.render_ffprobe
    streams = evidence.report.streams
    video = next((s for s in streams if s.codec_type == "video"), None)
    audio = next((s for s in streams if s.codec_type == "audio"), None)
    mismatches: list[FixedPresentationMismatch] = []

    def flag(code: str, detail: str) -> None:
        mismatches.append(FixedPresentationMismatch(code=code, detail=detail))

    if video is None:
        flag("render-video-missing", "no video stream in render output")
    else:
        pairs = (
            ("codec", video.codec_name, expected.video.codec_name),
            ("width", video.width, expected.video.width),
            ("height", video.height, expected.video.height),
            ("pix_fmt", video.pix_fmt, expected.video.pix_fmt),
            ("r_frame_rate", video.r_frame_rate, expected.video.r_frame_rate),
            ("avg_frame_rate", video.avg_frame_rate, expected.video.avg_frame_rate),
            ("nb_frames", video.nb_frames, expected.video.nb_frames),
        )
        for label, got, want in pairs:
            if got != want:
                flag("render-video-mismatch", f"{label} {got!r} != {want!r}")
        if video.duration is not None:
            want = Fraction(expected.format.duration.num, expected.format.duration.den)
            if _fraction(video.duration) != want:
                flag("render-video-mismatch", f"stream duration {video.duration} != {want}")
    if audio is None:
        flag("render-audio-missing", "no audio stream in render output")
    else:
        for label, got, want in (
            ("codec", audio.codec_name, expected.audio.codec_name),
            ("sample_rate", audio.sample_rate, expected.audio.sample_rate),
            ("channels", audio.channels, expected.audio.channels),
        ):
            if got != want:
                code = (
                    "audio-preset-mismatch"
                    if label in ("codec", "sample_rate", "channels")
                    else "render-audio-mismatch"
                )
                flag(code, f"render audio {label} {got!r} != {want!r}")
    fmt = evidence.report.format
    wanted_name = expected.format.format_name
    if wanted_name is not None and fmt.format_name != wanted_name:
        flag("render-format-mismatch", f"format_name {fmt.format_name!r} != {wanted_name!r}")
    if fmt.duration is not None and video is not None and video.duration is not None:
        want = Fraction(expected.format.duration.num, expected.format.duration.den)
        container = _fraction(fmt.duration)
        if not want <= container <= want + MAX_CONTAINER_PADDING_SECONDS:
            bound = want + MAX_CONTAINER_PADDING_SECONDS
            flag(
                "render-format-mismatch",
                f"container duration {fmt.duration} outside [{want}, {bound}]",
            )
    summary = (
        f"frames={video.nb_frames if video else '?'} "
        f"duration={video.duration if video else '?'}/{fmt.duration} "
        f"audio={audio.codec_name if audio else '?'}/{audio.sample_rate if audio else '?'}/"
        f"{audio.channels if audio else '?'}"
    )
    return summary, tuple(mismatches)
