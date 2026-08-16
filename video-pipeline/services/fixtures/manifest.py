from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel


def _tuple[Value](value: list[Value] | tuple[Value, ...]) -> tuple[Value, ...]:
    return tuple(value)


type Sequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(_tuple)]
type Frame = Annotated[int, Field(ge=0, strict=True)]
type Positive = Annotated[int, Field(gt=0, strict=True)]


class Rational(StrictModel):
    num: Positive
    den: Positive


class FrameSpan(StrictModel):
    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> FrameSpan:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("span_empty", "fixture spans must be non-empty")
        return self


class SourceRecipe(StrictModel):
    generator: Literal["ffmpeg-testsrc2-bars-v1"]
    duration_frames: Literal[600]
    frame_rate: Rational
    width: Literal[1920]
    height: Literal[1080]
    pixel_format: Literal["yuv420p"]

    @model_validator(mode="after")
    def require_cfr30(self) -> SourceRecipe:
        if (self.frame_rate.num, self.frame_rate.den) != (30, 1):
            raise PydanticCustomError("fixture_rate", "Phase 0A fixture requires exact CFR 30/1")
        return self


class AudioRecipe(StrictModel):
    generator: Literal["python-wave-pcm-pulse-v1"]
    sample_rate: Literal[48000]
    channels: Literal[1]
    sample_format: Literal["s16le"]
    duration_samples: Literal[960000]
    pulse_frames: Sequence[Frame]
    pulse_sample_positions: Sequence[Frame]
    pulse_width_samples: Literal[480]
    amplitude: Literal[16384]

    @model_validator(mode="after")
    def require_pulse_anchors(self) -> AudioRecipe:
        if self.pulse_frames != (0, 150, 300, 450):
            raise PydanticCustomError("pulse_frames", "Phase 0A pulse frames are fixed")
        if self.pulse_sample_positions != (0, 240000, 480000, 720000):
            raise PydanticCustomError("pulse_samples", "Phase 0A pulse samples are fixed")
        return self


class LinkedCut(StrictModel):
    item_id: Identifier
    source_span: FrameSpan
    record_span: FrameSpan
    video_track_index: Literal[1]
    audio_track_index: Literal[1]
    av_link_id: Identifier


class SlateRecipe(StrictModel):
    generator: Literal["ffmpeg-color-v1"]
    color: str
    duration_frames: Literal[30]
    audio: Literal["silence"]
    av_link_id: Identifier
    record_span: FrameSpan


class SubtitleRecipe(StrictModel):
    strategy: Literal["fixed-subtitle-v1"]
    text: Literal["PHASE 0A FIXED SUBTITLE"]
    track_index: Literal[1]
    record_span: FrameSpan


class RenderPreset(StrictModel):
    preset_id: Literal["phase-0a-h264-aac-v1"]
    container: Literal["mp4"]
    video_codec: Literal["h264"]
    video_pixel_format: Literal["yuv420p"]
    audio_codec: Literal["aac"]
    audio_sample_rate: Literal[48000]
    audio_channels: Literal[2]


class FixtureRecipe(StrictModel):
    source: SourceRecipe
    audio: AudioRecipe
    cuts: Sequence[LinkedCut]
    subtitle: SubtitleRecipe
    intro: SlateRecipe
    outro: SlateRecipe
    render_preset: RenderPreset

    @model_validator(mode="after")
    def require_fixed_layout(self) -> FixtureRecipe:
        expected = (
            ("cut-001", 0, 300, 30, 330, "av-cut-001"),
            ("cut-002", 300, 600, 330, 630, "av-cut-002"),
        )
        actual = tuple(
            (
                cut.item_id,
                cut.source_span.start_frame,
                cut.source_span.end_frame,
                cut.record_span.start_frame,
                cut.record_span.end_frame,
                cut.av_link_id,
            )
            for cut in self.cuts
        )
        if actual != expected:
            raise PydanticCustomError("fixed_cuts", "Phase 0A requires two fixed linked A/V cuts")
        return self


class ProbeVideo(StrictModel):
    width: Literal[1920]
    height: Literal[1080]
    pix_fmt: Literal["yuv420p"]
    r_frame_rate: Literal["30/1"]
    avg_frame_rate: Literal["30/1"]
    nb_frames: Literal["600", "660"]
    codec_name: Literal["h264"] | None = None


class ProbeAudio(StrictModel):
    codec_name: Literal["pcm_s16le", "aac"]
    sample_rate: Literal["48000"]
    channels: Literal[1, 2]


class ProbeFormat(StrictModel):
    duration: Rational
    format_name: Literal["mov,mp4,m4a,3gp,3g2,mj2"] | None = None


class ProbeExpectation(StrictModel):
    video: ProbeVideo
    audio: ProbeAudio
    format: ProbeFormat


class ReadbackItem(LinkedCut):
    pass


class ReadbackExpectation(StrictModel):
    record_frame_count: Literal[660]
    video_track_count: Literal[1]
    audio_track_count: Literal[1]
    subtitle_track_count: Literal[1]
    items: Sequence[ReadbackItem]


class FixtureExpected(StrictModel):
    source_ffprobe: ProbeExpectation
    readback: ReadbackExpectation
    render_ffprobe: ProbeExpectation


class Phase0AFixtureManifest(StrictModel):
    schema_version: Literal["fixture-manifest-v1"]
    fixture_id: Literal["p0a-cfr30-fixed"]
    phase: Literal["phase-0a"]
    recipe: FixtureRecipe
    expected: FixtureExpected

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(mode="json", exclude_none=True)
        return (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
            + b"\n"
        )
