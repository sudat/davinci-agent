from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel
from services.toolchain.normalization import VariantId  # noqa: TC001 (pydantic runtime)

type Sequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(tuple)]
type Frame = Annotated[int, Field(ge=0, strict=True)]
type Positive = Annotated[int, Field(gt=0, strict=True)]
type GenerationArgv = Annotated[tuple[str, ...], Field(min_length=1)]

PHASE_0B_FIXTURE_IDS: tuple[str, ...] = (
    "p0b-cfr24",
    "p0b-ntsc2997",
    "p0b-ntsc5994",
    "p0b-vfr-2-3-cadence",
    "p0b-rotate90",
    "p0b-audio-offset1024",
)


class Rational(StrictModel):
    num: Frame
    den: Positive


class FrameSpan(StrictModel):
    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> FrameSpan:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("span_empty", "fixture spans must be non-empty")
        return self


class GenerationVideo(StrictModel):
    generator: Literal["ffmpeg-testsrc2-lavfi-v1"]
    lavfi: str
    argv: GenerationArgv
    input_frames: Positive
    decoded_frames: Positive
    frame_rate: Rational
    width: Literal[1920]
    height: Literal[1080]
    pixel_format: Literal["yuv420p"]
    video_track_timescale: Positive
    fps_mode: Literal["cfr", "vfr"]

    @model_validator(mode="after")
    def require_resolution_consistency(self) -> GenerationVideo:
        if f"size={self.width}x{self.height}" not in self.lavfi:
            raise PydanticCustomError("lavfi_size", "lavfi source must match coded dimensions")
        if "{ffmpeg}" not in self.argv or "{output}" not in self.argv:
            raise PydanticCustomError("generation_argv", "generation argv needs ffmpeg/output")
        if self.decoded_frames > self.input_frames:
            raise PydanticCustomError("decoded_frames", "decoded frames exceed input frames")
        return self


class GenerationAudio(StrictModel):
    generator: Literal["python-wave-pcm-pulse-v1"]
    sample_rate: Literal[48000]
    channels: Literal[1]
    duration_samples: Positive
    pulse_frames: Sequence[Frame]
    pulse_sample_positions: Sequence[Frame]
    pulse_width_samples: Literal[480]
    amplitude: Literal[16384]
    content_offset_samples: Frame

    @model_validator(mode="after")
    def require_pulse_alignment(self) -> GenerationAudio:
        if len(self.pulse_frames) != len(self.pulse_sample_positions):
            raise PydanticCustomError("pulse_count", "pulse frames and samples must align")
        if self.pulse_sample_positions[0] < self.content_offset_samples:
            raise PydanticCustomError("pulse_offset", "first pulse must respect content offset")
        return self


class PostProcessStep(StrictModel):
    generator: Literal["python-mov-tkhd-rotate90-v1"]
    rotation_degrees: Literal[90]


class GenerationRecipe(StrictModel):
    video: GenerationVideo
    audio: GenerationAudio
    post: Sequence[PostProcessStep]
    notes: tuple[str, ...]


class FrameRateTable(StrictModel):
    frame_rate: Rational
    frame_duration_seconds: Rational
    duration_seconds: Rational
    samples_per_frame_numerator: Positive
    samples_per_frame_denominator: Positive


class ConversionExpectation(StrictModel):
    target_frame_rate: Rational
    input_frames: Positive
    output_frames: Positive
    dropped_source_frames: Sequence[Frame]
    duplicated_source_frames: Sequence[Frame]

    @model_validator(mode="after")
    def require_accounting(self) -> ConversionExpectation:
        shown = self.input_frames - len(self.dropped_source_frames)
        span = self.output_frames - shown
        if span != len(self.duplicated_source_frames):
            raise PydanticCustomError("drop_dup_accounting", "drop/dup counts must balance")
        return self


class TickSpan(StrictModel):
    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> TickSpan:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("tick_span_empty", "tick spans must be non-empty")
        return self


class SubtitleAnchor(StrictModel):
    cue_id: str
    text: str
    source_frame_span: FrameSpan
    cfr30_tick_span: TickSpan
    cfr24_tick_span: TickSpan


class AudioSampleAnchor(StrictModel):
    pulse_id: str
    source_frame: Frame
    source_sample: Frame
    cfr30_tick: Frame | None
    cfr30_sample: Frame
    cfr24_tick: Frame | None
    cfr24_sample: Frame


class ReadbackMarker(StrictModel):
    marker_id: str
    source_frame: Frame
    source_time_seconds: Rational
    cfr30_frame: Frame | None
    cfr24_frame: Frame | None
    audio_sample: Frame


class RotationExpectation(StrictModel):
    rotation_degrees: Literal[90]
    coded_width: Literal[1920]
    coded_height: Literal[1080]
    display_width: Literal[1080]
    display_height: Literal[1920]


class ProbeVideoExpectation(StrictModel):
    r_frame_rate: str
    avg_frame_rate: str
    nb_frames: str
    width: Literal[1920]
    height: Literal[1080]
    pix_fmt: Literal["yuv420p"]


class ProbeAudioExpectation(StrictModel):
    codec_name: Literal["pcm_s16le"]
    sample_rate: Literal["48000"]
    channels: Literal[1]


class ProbeFormatExpectation(StrictModel):
    duration_seconds: Rational


class ProbeExpectation(StrictModel):
    video: ProbeVideoExpectation
    audio: ProbeAudioExpectation
    format: ProbeFormatExpectation


class Phase0BFixtureManifest(StrictModel):
    schema_version: Literal["phase-0b-fixture-manifest-v1"]
    fixture_id: VariantId
    phase: Literal["phase-0b"]
    generation: GenerationRecipe
    rational_frame_rate_table: FrameRateTable
    conversions: dict[Literal["cfr30", "cfr24"], ConversionExpectation]
    subtitle_anchors: tuple[SubtitleAnchor, ...]
    audio_sample_anchors: tuple[AudioSampleAnchor, ...]
    resolve_readback: tuple[ReadbackMarker, ...]
    rotation: RotationExpectation | None = None
    expected_ffprobe: ProbeExpectation
    expectation_basis: Literal["rational-arithmetic-pre-registered"]

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(mode="json")
        return (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
            + b"\n"
        )

    @model_validator(mode="after")
    def reject_observed_result_fields(self) -> Phase0BFixtureManifest:
        stack: list[object] = [self.model_dump(mode="json", exclude_none=True)]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if "observed" in key:
                        raise PydanticCustomError(
                            "observed_result_field",
                            "manifests pre-register rational expectations; observed outputs "
                            "are not manifest fields",
                        )
                    stack.append(value)
            elif isinstance(node, list | tuple):
                stack.extend(node)
        return self
