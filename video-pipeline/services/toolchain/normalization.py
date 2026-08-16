from __future__ import annotations

import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services.toolchain.smoke import ProbeAssertionError, ProbePayload

FROZEN_FILTERS: tuple[str, ...] = (
    "testsrc2",
    "color",
    "scale",
    "fps",
    "aresample",
    "asetpts",
    "setpts",
    "overlay",
)
FROZEN_ENCODERS: tuple[str, ...] = ("h264_videotoolbox", "aac", "pcm_s16le")

type VariantId = Literal[
    "p0b-cfr24",
    "p0b-ntsc2997",
    "p0b-ntsc5994",
    "p0b-vfr-2-3-cadence",
    "p0b-rotate90",
    "p0b-audio-offset1024",
]

PHASE_0B_VARIANT_ORDER: tuple[str, ...] = (
    "p0b-cfr24",
    "p0b-ntsc2997",
    "p0b-ntsc5994",
    "p0b-vfr-2-3-cadence",
    "p0b-rotate90",
    "p0b-audio-offset1024",
)

type Argv = Annotated[tuple[str, ...], Field(min_length=1)]


def _validate_common_placeholders(argv: tuple[str, ...]) -> tuple[str, ...]:
    if "{ffmpeg}" not in argv:
        msg = "recipe argv is missing {ffmpeg}"
        raise ValueError(msg)
    return argv


def _validate_io_placeholders(argv: tuple[str, ...]) -> tuple[str, ...]:
    joined = "\n".join(argv)
    for placeholder in ("{input}", "{output}"):
        if placeholder not in joined:
            msg = f"recipe argv is missing {placeholder}"
            raise ValueError(msg)
    return argv


def _validate_output_placeholder(argv: tuple[str, ...]) -> tuple[str, ...]:
    if "{output}" not in argv:
        msg = "generator argv is missing {output}"
        raise ValueError(msg)
    return argv


def _validate_frozen_tools(argv: tuple[str, ...]) -> tuple[str, ...]:
    for flag in ("-vf", "-af"):
        for index, token in enumerate(argv):
            if token != flag:
                continue
            for stage in argv[index + 1].split(","):
                _require_frozen_filter(stage)
    for index, token in enumerate(argv):
        if token in ("-c:v", "-c:a") and argv[index + 1] not in FROZEN_ENCODERS:
            msg = f"encoder is outside the frozen 0A set: {argv[index + 1]}"
            raise ValueError(msg)
    return argv


def _require_frozen_filter(stage: str) -> None:
    name = stage.split("=", maxsplit=1)[0].strip()
    if name not in FROZEN_FILTERS:
        msg = f"filter is outside the frozen 0A set: {name}"
        raise ValueError(msg)


class NormalizationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Rational(NormalizationModel):
    num: int = Field(gt=0)
    den: int = Field(gt=0)


class NormalizeRecipe(NormalizationModel):
    fixture_id: VariantId
    argv: Argv

    _common = field_validator("argv")(_validate_common_placeholders)
    _io = field_validator("argv")(_validate_io_placeholders)
    _frozen = field_validator("argv")(_validate_frozen_tools)


class NormalizeTarget(NormalizationModel):
    frame_rate: Rational
    sample_rate: Literal[48000]
    video_codec: Literal["h264_videotoolbox"]
    audio_codec: Literal["pcm_s16le"]
    container: Literal["mov"]
    video_track_timescale: Literal[30000]

    @field_validator("frame_rate")
    @classmethod
    def require_cfr30(cls, rate: Rational) -> Rational:
        if (rate.num, rate.den) != (30, 1):
            msg = "0B normalization target must be CFR 30/1"
            raise ValueError(msg)
        return rate


class NormalizeSmokeExpectation(NormalizationModel):
    avg_frame_rate: Literal["30/1"]
    nb_read_frames: int = Field(gt=0)
    duration_milliseconds: int = Field(gt=0)


class NormalizeSmokeProfile(NormalizationModel):
    profile_id: Literal["ffmpeg-normalize"]
    recipe_fixture_id: VariantId
    generator_argv: Argv
    expected: NormalizeSmokeExpectation

    _common = field_validator("generator_argv")(_validate_common_placeholders)
    _output = field_validator("generator_argv")(_validate_output_placeholder)
    _frozen = field_validator("generator_argv")(_validate_frozen_tools)


class NormalizationSection(NormalizationModel):
    schema_version: Literal["normalize-recipes-v1"]
    target: NormalizeTarget
    recipes: tuple[NormalizeRecipe, ...]
    smoke: NormalizeSmokeProfile

    @field_validator("recipes")
    @classmethod
    def require_all_variants(
        cls, recipes: tuple[NormalizeRecipe, ...]
    ) -> tuple[NormalizeRecipe, ...]:
        ids = tuple(recipe.fixture_id for recipe in recipes)
        if ids != PHASE_0B_VARIANT_ORDER:
            msg = f"normalization must pin exactly the six 0B variants in order: {ids}"
            raise ValueError(msg)
        return recipes


def _substitute(argv: tuple[str, ...], ffmpeg: str, source: str, destination: str) -> list[str]:
    mapping = {"{ffmpeg}": ffmpeg, "{input}": source, "{output}": destination}
    return [mapping.get(token, token) for token in argv]


def _assert_smoke(section: NormalizationSection, stdout: str) -> None:
    try:
        payload = ProbePayload.model_validate_json(stdout)
    except ValidationError as error:
        raise ProbeAssertionError(f"invalid ffprobe payload: {error}") from error
    video = next((stream for stream in payload.streams if stream.codec_type == "video"), None)
    if video is None:
        raise ProbeAssertionError("ffprobe payload has no video stream")
    frame_count_text = video.nb_read_frames or video.nb_frames
    if frame_count_text is None:
        raise ProbeAssertionError("ffprobe payload has no frame count")
    try:
        frame_count = int(frame_count_text)
        duration = Decimal(video.duration or payload.format.duration)
    except (InvalidOperation, ValueError) as error:
        raise ProbeAssertionError("ffprobe duration or frame count is malformed") from error
    expected = section.smoke.expected
    if (
        video.avg_frame_rate != expected.avg_frame_rate
        or frame_count != expected.nb_read_frames
        or int(duration * 1000) != expected.duration_milliseconds
    ):
        raise ProbeAssertionError("ffmpeg-normalize smoke expectation failed")


def run_normalize_smoke(
    ffmpeg: str,
    ffprobe: str,
    section: NormalizationSection,
    smoke_dir: Path,
) -> tuple[Path, ...]:
    smoke_dir.mkdir(parents=True, exist_ok=True)
    source = smoke_dir / "normalize-smoke-input.mov"
    destination = smoke_dir / "normalize-smoke-output.mov"
    probe = smoke_dir / "normalize-smoke-ffprobe.json"
    generator = _substitute(section.smoke.generator_argv, ffmpeg, str(source), str(source))
    recipe = next(
        item for item in section.recipes if item.fixture_id == section.smoke.recipe_fixture_id
    )
    normalize = _substitute(recipe.argv, ffmpeg, str(source), str(destination))
    subprocess.run(generator, check=True)
    subprocess.run(normalize, check=True)
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _assert_smoke(section, result.stdout)
    probe.write_text(result.stdout, encoding="utf-8")
    return source, destination, probe
