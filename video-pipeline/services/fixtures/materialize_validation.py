from __future__ import annotations

import sys
import wave
from array import array
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from services.contracts.primitives import Sha256, StrictModel
from services.fixtures.manifest import (  # noqa: TC001 - Pydantic resolves fields at runtime
    Rational,
    ReadbackItem,
    SubtitleRecipe,
)

if TYPE_CHECKING:
    from services.fixtures.manifest import (
        AudioRecipe,
        Phase0AFixtureManifest,
        ProbeExpectation,
    )

SAMPLE_WIDTH_BYTES = 2


@dataclass(frozen=True, slots=True)
class MaterializationError(Exception):
    detail: str

    def __str__(self) -> str:
        return self.detail


class RawProbeStream(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    codec_type: Literal["video", "audio"]
    codec_name: str
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    r_frame_rate: str | None = None
    avg_frame_rate: str | None = None
    nb_frames: str | None = None
    sample_rate: str | None = None
    channels: int | None = None


class RawProbeFormat(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    duration: str
    format_name: str


class RawProbeDocument(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    streams: tuple[RawProbeStream, ...]
    format: RawProbeFormat


class ProbeObservation(StrictModel):
    video_codec: str
    width: int
    height: int
    pixel_format: str
    r_frame_rate: str
    avg_frame_rate: str
    frame_count: str
    audio_codec: str
    audio_sample_rate: str
    audio_channels: int
    format_name: str
    duration: str


class ArtifactHash(StrictModel):
    name: str
    sha256: Sha256


class AudioPreset(StrictModel):
    audio_codec: Literal["aac"]
    audio_sample_rate: Literal[48000]
    audio_channels: Literal[2]


class MaterializedTimeline(StrictModel):
    frame_rate: Rational
    items: tuple[ReadbackItem, ...]
    subtitle: SubtitleRecipe


class MaterializationReport(StrictModel):
    schema_version: Literal["phase-0a-materialization-report-v1"]
    fixture_id: Literal["p0a-cfr30-fixed"]
    policy_sha256: Sha256
    fixture_manifest_sha256: Sha256
    golden_expected_sha256: Sha256
    determinism_path: Literal["semantic-equivalence-h264-videotoolbox"]
    codec_variability: str
    source_probe: ProbeObservation
    intro_probe: ProbeObservation
    outro_probe: ProbeObservation
    source_decoded_video_sha256: Sha256
    intro_decoded_video_sha256: Sha256
    outro_decoded_video_sha256: Sha256
    artifacts: tuple[ArtifactHash, ...]
    validation: Literal["passed"]


def parse_probe(raw: str) -> ProbeObservation:
    document = RawProbeDocument.model_validate_json(raw)
    video = next((stream for stream in document.streams if stream.codec_type == "video"), None)
    audio = next((stream for stream in document.streams if stream.codec_type == "audio"), None)
    if video is None or audio is None:
        raise MaterializationError("ffprobe requires one video and one audio stream")
    required_video = (
        video.width,
        video.height,
        video.pix_fmt,
        video.r_frame_rate,
        video.avg_frame_rate,
        video.nb_frames,
    )
    if any(value is None for value in required_video):
        raise MaterializationError("ffprobe video metadata is incomplete")
    if video.width is None or video.height is None or video.pix_fmt is None:
        raise MaterializationError("ffprobe video dimensions are incomplete")
    if video.r_frame_rate is None or video.avg_frame_rate is None or video.nb_frames is None:
        raise MaterializationError("ffprobe video timing is incomplete")
    if audio.sample_rate is None or audio.channels is None:
        raise MaterializationError("ffprobe audio metadata is incomplete")
    return ProbeObservation(
        video_codec=video.codec_name,
        width=video.width,
        height=video.height,
        pixel_format=video.pix_fmt,
        r_frame_rate=video.r_frame_rate,
        avg_frame_rate=video.avg_frame_rate,
        frame_count=video.nb_frames,
        audio_codec=audio.codec_name,
        audio_sample_rate=audio.sample_rate,
        audio_channels=audio.channels,
        format_name=document.format.format_name,
        duration=document.format.duration,
    )


def validate_probe(observed: ProbeObservation, expected: ProbeExpectation) -> None:
    expected_duration = Decimal(expected.format.duration.num) / Decimal(
        expected.format.duration.den
    )
    try:
        observed_duration = Decimal(observed.duration)
    except InvalidOperation as error:
        raise MaterializationError("ffprobe duration is malformed") from error
    checks = (
        observed.width == expected.video.width,
        observed.height == expected.video.height,
        observed.pixel_format == expected.video.pix_fmt,
        observed.r_frame_rate == expected.video.r_frame_rate,
        observed.avg_frame_rate == expected.video.avg_frame_rate,
        observed.frame_count == expected.video.nb_frames,
        observed.audio_codec == expected.audio.codec_name,
        observed.audio_sample_rate == expected.audio.sample_rate,
        observed.audio_channels == expected.audio.channels,
        observed_duration == expected_duration,
    )
    if expected.video.codec_name is not None:
        checks += (observed.video_codec == expected.video.codec_name,)
    if expected.format.format_name is not None:
        checks += (observed.format_name == expected.format.format_name,)
    if not all(checks):
        raise MaterializationError("ffprobe output differs from frozen expected values")


def validate_slate(observed: ProbeObservation, manifest: Phase0AFixtureManifest) -> None:
    expected_duration = Decimal(manifest.recipe.intro.duration_frames) / Decimal(
        manifest.recipe.source.frame_rate.num
    )
    if (
        observed.width != manifest.recipe.source.width
        or observed.height != manifest.recipe.source.height
        or observed.pixel_format != manifest.recipe.source.pixel_format
        or observed.r_frame_rate != "30/1"
        or observed.avg_frame_rate != "30/1"
        or observed.frame_count != "30"
        or observed.audio_codec != "pcm_s16le"
        or observed.audio_sample_rate != str(manifest.recipe.audio.sample_rate)
        or observed.audio_channels != manifest.recipe.audio.channels
        or Decimal(observed.duration) != expected_duration
    ):
        raise MaterializationError("generated slate differs from frozen recipe")


def validate_pulse(path: Path, recipe: AudioRecipe) -> None:
    with wave.open(str(path), "rb") as stream:
        if (
            stream.getframerate() != recipe.sample_rate
            or stream.getnchannels() != recipe.channels
            or stream.getsampwidth() != SAMPLE_WIDTH_BYTES
            or stream.getnframes() != recipe.duration_samples
        ):
            raise MaterializationError("pulse WAV format differs from frozen recipe")
        samples = array("h", stream.readframes(recipe.duration_samples))
    if sys.byteorder != "little":
        samples.byteswap()
    cursor = 0
    for position in recipe.pulse_sample_positions:
        if any(samples[cursor:position]):
            raise MaterializationError("unexpected nonzero samples before pulse")
        end = position + recipe.pulse_width_samples
        if any(sample != recipe.amplitude for sample in samples[position:end]):
            raise MaterializationError("pulse amplitude or width drift")
        cursor = end
    if any(samples[cursor:]):
        raise MaterializationError("unexpected nonzero samples after pulse")


def subtitle_bytes(manifest: Phase0AFixtureManifest) -> bytes:
    subtitle = manifest.recipe.subtitle
    rate = manifest.recipe.source.frame_rate.num
    start_seconds = subtitle.record_span.start_frame // rate
    end_seconds = subtitle.record_span.end_frame // rate
    return (
        f"1\n00:00:{start_seconds:02d},000 --> 00:00:{end_seconds:02d},000\n"
        f"{subtitle.text}\n"
    ).encode()
