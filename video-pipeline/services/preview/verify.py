"""Strict ffprobe verification of a produced preview (drift detection).

The renderer trusts no exit code; the pinned ffprobe re-reads the file and
asserts codec, size, frame rate, decoded frame count, video duration within
``MAX_DURATION_DRIFT_MS`` of the frame-exact RATIONAL duration (integer-frame
timelines at 30 fps are not whole milliseconds — e.g. 614 frames — so exact
integer-ms equality would reject correct outputs), stream layout, and the AAC
container padding tolerance from Todo 17.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.preview.models import FfprobeSummary, PreviewVerificationError
from services.preview.tools import PinnedTools, ProbeStream, probe_file

if TYPE_CHECKING:
    from services.contracts.primitives import RationalFrameRate

PREVIEW_WIDTH: Final = 640
PREVIEW_HEIGHT: Final = 360
AUDIO_SAMPLE_RATE_HZ: Final = 48000
AUDIO_SAMPLE_RATE_TEXT: Final = "48000"
MAX_CONTAINER_PADDING_MS: Final = 500
MAX_DURATION_DRIFT_MS: Final = Fraction(2)


def _duration_fraction(text: str | None, label: str) -> Fraction:
    if text is None:
        raise PreviewVerificationError(f"{label} duration missing from ffprobe")
    return Fraction(text) * 1000


@dataclass(frozen=True, slots=True)
class _ProbeFacts:
    video: ProbeStream
    audio: ProbeStream
    subtitle: ProbeStream | None
    stream_count: int
    video_ms: Fraction
    container_ms: Fraction
    frame_count: str | None


def _video_mismatches(
    facts: _ProbeFacts, rate_text: str, total_frames: int, expected_ms: Fraction
) -> list[str]:
    video = facts.video
    mismatches: list[str] = []
    if video.codec_name != "h264":
        mismatches.append(f"video codec {video.codec_name} != h264")
    if video.width != PREVIEW_WIDTH or video.height != PREVIEW_HEIGHT:
        mismatches.append(f"video size {video.width}x{video.height} != 640x360")
    if video.r_frame_rate != rate_text or video.avg_frame_rate != rate_text:
        mismatches.append(f"frame rate {video.r_frame_rate}/{video.avg_frame_rate} != {rate_text}")
    if facts.frame_count != str(total_frames):
        mismatches.append(f"frame count {facts.frame_count} != {total_frames}")
    if abs(facts.video_ms - expected_ms) > MAX_DURATION_DRIFT_MS:
        mismatches.append(
            f"video duration {float(facts.video_ms):.3f}ms != expected "
            f"{float(expected_ms):.3f}ms (±{float(MAX_DURATION_DRIFT_MS):.0f}ms)"
        )
    return mismatches


def _audio_subtitle_mismatches(
    audio: ProbeStream,
    subtitle: ProbeStream | None,
    stream_count: int,
    *,
    subtitle_expected: bool,
) -> list[str]:
    mismatches: list[str] = []
    if audio.codec_name != "aac" or audio.sample_rate != AUDIO_SAMPLE_RATE_TEXT:
        mismatches.append(f"audio {audio.codec_name}/{audio.sample_rate} != aac/48000")
    if audio.channels != 1:
        mismatches.append(f"audio channels {audio.channels} != 1")
    if subtitle_expected and (subtitle is None or subtitle.codec_name != "mov_text"):
        mismatches.append("expected a mov_text subtitle stream")
    if not subtitle_expected and subtitle is not None:
        mismatches.append(f"unexpected subtitle stream {subtitle.codec_name}")
    expected_streams = 3 if subtitle_expected else 2
    if stream_count != expected_streams:
        mismatches.append(f"stream count {stream_count} != {expected_streams}")
    return mismatches


def _stream_mismatches(
    facts: _ProbeFacts,
    rate_text: str,
    total_frames: int,
    expected_ms: Fraction,
    *,
    subtitle_expected: bool,
) -> list[str]:
    mismatches = _video_mismatches(facts, rate_text, total_frames, expected_ms)
    mismatches += _audio_subtitle_mismatches(
        facts.audio, facts.subtitle, facts.stream_count, subtitle_expected=subtitle_expected
    )
    lower = expected_ms - MAX_DURATION_DRIFT_MS
    upper = expected_ms + MAX_CONTAINER_PADDING_MS + MAX_DURATION_DRIFT_MS
    if not lower <= facts.container_ms <= upper:
        mismatches.append(
            f"container duration {float(facts.container_ms):.3f}ms outside "
            f"[{float(lower):.3f}, {float(upper):.3f}]ms (AAC container padding tolerance)"
        )
    return mismatches


def verify_preview_output(
    tools: PinnedTools,
    output: Path,
    *,
    total_frames: int,
    rate: RationalFrameRate,
    subtitle_expected: bool,
) -> FfprobeSummary:
    tools.verify_current()
    report = probe_file(tools, output, count_frames=True)
    video = next((s for s in report.streams if s.codec_type == "video"), None)
    audio = next((s for s in report.streams if s.codec_type == "audio"), None)
    subtitle = next((s for s in report.streams if s.codec_type == "subtitle"), None)
    if video is None or audio is None:
        raise PreviewVerificationError(f"preview lacks a video/audio stream: {output}")
    expected_ms = Fraction(total_frames * 1000 * rate.den, rate.num)
    facts = _ProbeFacts(
        video=video,
        audio=audio,
        subtitle=subtitle,
        stream_count=len(report.streams),
        video_ms=_duration_fraction(video.duration, "video"),
        container_ms=_duration_fraction(
            report.format.duration if report.format is not None else None, "container"
        ),
        frame_count=video.nb_read_frames or video.nb_frames,
    )
    mismatches = _stream_mismatches(
        facts,
        f"{rate.num}/{rate.den}",
        total_frames,
        expected_ms,
        subtitle_expected=subtitle_expected,
    )
    if mismatches:
        raise PreviewVerificationError(f"preview drift detected: {'; '.join(mismatches)}")
    return FfprobeSummary(
        stream_count=len(report.streams),
        video_codec=video.codec_name or "",
        width=video.width or 0,
        height=video.height or 0,
        r_frame_rate=video.r_frame_rate or "",
        avg_frame_rate=video.avg_frame_rate or "",
        nb_read_frames=int(facts.frame_count or "0"),
        video_duration_ms=round(facts.video_ms),
        container_duration_ms=round(facts.container_ms),
        audio_codec=audio.codec_name or "",
        audio_sample_rate=int(audio.sample_rate or "0"),
        audio_channels=audio.channels or 0,
        subtitle_codec=subtitle.codec_name if subtitle is not None else None,
    )


__all__ = [
    "AUDIO_SAMPLE_RATE_TEXT",
    "MAX_CONTAINER_PADDING_MS",
    "PREVIEW_HEIGHT",
    "PREVIEW_WIDTH",
    "verify_preview_output",
]
