from __future__ import annotations

import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel
from services.foundation_io import atomic_write
from services.toolchain.normalization import FROZEN_ENCODERS, FROZEN_FILTERS
from services.toolchain.smoke import ProbeAssertionError, ProbePayload

type Argv = Annotated[tuple[str, ...], Field(min_length=1)]


def _require_placeholders(argv: tuple[str, ...]) -> tuple[str, ...]:
    joined = "\n".join(argv)
    for placeholder in ("{ffmpeg}", "{output}"):
        if placeholder not in joined:
            msg = f"preview smoke argv is missing {placeholder}"
            raise ValueError(msg)
    return argv


def _require_frozen_tools(argv: tuple[str, ...]) -> tuple[str, ...]:
    for flag in ("-vf", "-af"):
        for index, token in enumerate(argv):
            if token != flag:
                continue
            for stage in argv[index + 1].split(","):
                name = stage.split("=", maxsplit=1)[0].strip()
                if name not in FROZEN_FILTERS:
                    msg = f"filter is outside the frozen 0A set: {name}"
                    raise ValueError(msg)
    for index, token in enumerate(argv):
        if token in ("-c:v", "-c:a") and argv[index + 1] not in FROZEN_ENCODERS:
            msg = f"encoder is outside the frozen 0A set: {argv[index + 1]}"
            raise ValueError(msg)
    return argv


class PreviewSmokeExpectation(StrictModel):
    avg_frame_rate: Literal["30/1"]
    nb_read_frames: int = Field(gt=0, strict=True)
    duration_milliseconds: int = Field(gt=0, strict=True)


class PreviewSmokeProfile(StrictModel):
    profile_id: Literal["preview-review"]
    generator_argv: Argv
    expected: PreviewSmokeExpectation

    _placeholders = field_validator("generator_argv")(_require_placeholders)
    _frozen = field_validator("generator_argv")(_require_frozen_tools)


class PreviewReviewSection(StrictModel):
    schema_version: Literal["preview-review-v1"]
    adapter: Literal["pinned-ffmpeg-preview"]
    review_translator_policy_profile_id: Identifier
    external_model: Literal["none"]
    smoke: PreviewSmokeProfile


def _substitute(argv: tuple[str, ...], ffmpeg: str, destination: str) -> list[str]:
    mapping = {"{ffmpeg}": ffmpeg, "{output}": destination}
    return [mapping.get(token, token) for token in argv]


def _assert_smoke(section: PreviewReviewSection, stdout: str) -> None:
    try:
        payload = ProbePayload.model_validate_json(stdout)
    except Exception as error:
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
        raise ProbeAssertionError("preview-review smoke expectation failed")


def run_preview_smoke(
    ffmpeg: str,
    ffprobe: str,
    section: PreviewReviewSection,
    smoke_dir: Path,
) -> tuple[Path, ...]:
    smoke_dir.mkdir(parents=True, exist_ok=True)
    encoded = smoke_dir / "preview-smoke.mp4"
    probe = smoke_dir / "preview-smoke-ffprobe.json"
    generator = _substitute(section.smoke.generator_argv, ffmpeg, str(encoded))
    subprocess.run(generator, check=True)
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
            str(encoded),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _assert_smoke(section, result.stdout)
    atomic_write(probe, result.stdout.encode())
    return encoded, probe


def assert_no_external_model(section: PreviewReviewSection) -> None:
    if section.external_model != "none":
        raise PydanticCustomError(
            "external_model_forbidden",
            "Phase 0C pins no external model; the translator contract is deterministic",
        )
