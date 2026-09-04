"""Strict ffprobe stream facts for the chapter-card master."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError

from services.cli._v44_chapter_card_media import ChapterCardMediaError, run_pinned

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools

PROBE_TIMEOUT_SECONDS: Final = 600


@dataclass(frozen=True, slots=True)
class MasterFacts:
    video_codec: str
    video_time_base: str
    video_frames: int
    avg_frame_rate: str
    width: int
    height: int
    audio_codec: str
    sample_rate: int
    channels: int
    audio_samples: int

    @property
    def video_timescale(self) -> int:
        """Denominator of the measured stream time base, e.g. 15360 for 1/15360."""

        _, _, denominator = self.video_time_base.partition("/")
        return int(denominator)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, (str, int)):
        raise TypeError(f"not an ffprobe number: {value!r}")
    return int(value)


type ProbeValue = int | None


class _ProbeStream(BaseModel):
    """Projected ffprobe stream: unknown keys ignored, numeric strings coerced."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    codec_type: str
    codec_name: str | None = None
    time_base: str | None = None
    avg_frame_rate: str | None = None
    nb_read_frames: Annotated[ProbeValue, BeforeValidator(_optional_int)] = None
    width: Annotated[ProbeValue, BeforeValidator(_optional_int)] = None
    height: Annotated[ProbeValue, BeforeValidator(_optional_int)] = None
    sample_rate: Annotated[ProbeValue, BeforeValidator(_optional_int)] = None
    channels: Annotated[ProbeValue, BeforeValidator(_optional_int)] = None
    duration_ts: Annotated[ProbeValue, BeforeValidator(_optional_int)] = None


class _ProbeReport(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    streams: tuple[_ProbeStream, ...] = ()


def _required_int(stream: _ProbeStream, field: str) -> int:
    value: int | None = getattr(stream, field)
    if value is None:
        raise ChapterCardMediaError(
            "probe-malformed", f"{stream.codec_type} stream lacks {field}"
        )
    return value


def probe_master_facts(tools: PinnedTools, path: Path) -> MasterFacts:
    """Probe the master with -count_frames for authoritative stream facts."""

    payload = run_pinned(
        tools,
        (
            str(tools.ffprobe),
            "-v",
            "error",
            "-count_frames",
            "-print_format",
            "json",
            "-show_streams",
            str(path),
        ),
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    try:
        report = _ProbeReport.model_validate_json(payload)
    except ValidationError as error:
        raise ChapterCardMediaError(
            "probe-malformed", f"ffprobe payload unreadable: {error}"
        ) from error
    video = next((s for s in report.streams if s.codec_type == "video"), None)
    audio = next((s for s in report.streams if s.codec_type == "audio"), None)
    if video is None or audio is None:
        raise ChapterCardMediaError("probe-streams-missing", f"no video/audio stream in {path}")
    if video.codec_name is None or video.time_base is None or video.avg_frame_rate is None:
        raise ChapterCardMediaError(
            "probe-malformed", f"video stream lacks codec/time-base fields in {path}"
        )
    if audio.codec_name is None:
        raise ChapterCardMediaError("probe-malformed", f"audio stream lacks codec name in {path}")
    return MasterFacts(
        video_codec=video.codec_name,
        video_time_base=video.time_base,
        video_frames=_required_int(video, "nb_read_frames"),
        avg_frame_rate=video.avg_frame_rate,
        width=_required_int(video, "width"),
        height=_required_int(video, "height"),
        audio_codec=audio.codec_name,
        sample_rate=_required_int(audio, "sample_rate"),
        channels=_required_int(audio, "channels"),
        audio_samples=_required_int(audio, "duration_ts"),
    )


__all__ = ["MasterFacts", "probe_master_facts"]
