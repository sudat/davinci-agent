"""Typed contracts for the pinned local Japanese ASR adapter (whisper.cpp CLI).

Canonical artifact fields never use floats: whisper segment timestamps are the
integer-millisecond ``offsets`` values emitted by the pinned whisper-cli JSON
writer. Token/word-level timing is carried ONLY under an explicit experimental
label and is never trusted as canonical evidence.

The pinned build is compiled with ``-DGGML_METAL=OFF`` (CPU-only by
construction, per the frozen toolchain lock); the adapter passes no
accelerator-force flag, so CPU (and temperature) fallback stays internal to
whisper.cpp.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    ArtifactEnvelope,
    Producer,
    Sha256,
    StrictModel,
)

FROZEN_PREPROCESSING_ARGV: Final = (
    "-nostdin",
    "-i",
    "{input}",
    "-vn",
    "-ar",
    "16000",
    "-ac",
    "1",
    "-c:a",
    "pcm_s16le",
    "{output}",
)
FROZEN_WHISPER_ARGV_TEMPLATE: Final = (
    "{whisper_cli}",
    "--model",
    "{model}",
    "--language",
    "ja",
    "--temperature",
    "0.000",
    "--threads",
    "4",
    "--no-prints",
    "--output-json-full",
    "--file",
    "{wav}",
    "--output-file",
    "{out_base}",
)
FROZEN_TEMPERATURE: Final = 0
FROZEN_THREADS: Final = 4
FORBIDDEN_ACCELERATOR_FLAGS: Final = frozenset(
    (
        "-ng",
        "--no-gpu",
        "-dev",
        "--device",
        "-fa",
        "--flash-attn",
        "-nfa",
        "--no-flash-attn",
        "-nf",
        "--no-fallback",
        "-tr",
        "--translate",
    )
)
TRANSCRIPT_PRODUCER: Final = Producer(name="asr-whisper-cpp", version="todo33-v1")


class AsrError(Exception):
    """Base adapter error; carries a machine-readable label."""

    label: str = "asr_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.label}: {self.detail}"


class AsrRequestError(AsrError):
    """Refused before execution: non-local input, PATH tool, settings drift."""

    label = "asr_request_refused"


class AsrToolDriftError(AsrError):
    """A pinned binary/model/input no longer matches the frozen lock."""

    label = "asr_tool_drift"


class AsrExecutionError(AsrError):
    """A bounded subprocess failed or timed out."""

    label = "asr_execution_failed"


class AsrWavProbeError(AsrError):
    """The preprocessed WAV did not probe as 16 kHz mono pcm_s16le."""

    label = "asr_wav_probe_mismatch"


class AsrParseError(AsrError):
    """whisper-cli output could not be parsed into the contract."""

    label = "asr_cli_output_malformed"


class AsrCacheConflictError(AsrError):
    """A stale/foreign cache entry occupies the key; overwrite refused."""

    label = "asr_cache_conflict"


class PinnedTool(StrictModel):
    path: str
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def require_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError(f"tool path must be absolute, got {value!r}")
        return value


class AsrRequest(StrictModel):
    input_media_path: str
    input_media_sha256: Sha256
    language: Literal["ja"] = "ja"
    no_translate: Literal[True] = True
    temperature: int = Field(default=FROZEN_TEMPERATURE, ge=0, le=1, strict=True)
    threads: int = Field(default=FROZEN_THREADS, ge=1, le=1024, strict=True)
    output_json: Literal[True] = True
    model: PinnedTool
    cli: PinnedTool
    ffmpeg: PinnedTool

    @field_validator("input_media_path")
    @classmethod
    def require_local_file_path(cls, value: str) -> str:
        if "://" in value or not Path(value).is_absolute():
            raise ValueError(
                f"input media must be an absolute local file path, got {value!r}"
            )
        return value


class WavProbeSummary(StrictModel):
    sample_rate: Literal[16000] = 16000
    channels: Literal[1] = 1
    codec: Literal["pcm_s16le"] = "pcm_s16le"


class AsrInputBinding(StrictModel):
    media_path: str
    media_sha256: Sha256
    wav_sha256: Sha256
    wav_probe: WavProbeSummary


class TranscriptSegment(StrictModel):
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(ge=0, strict=True)
    text: str

    @model_validator(mode="after")
    def require_forward_span(self) -> TranscriptSegment:
        if self.end_ms < self.start_ms:
            raise PydanticCustomError("span_inverted", "end_ms must be >= start_ms")
        return self


class ExperimentalToken(StrictModel):
    text: str
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(ge=0, strict=True)


class SegmentTokenTiming(StrictModel):
    segment_index: int = Field(ge=0, strict=True)
    tokens: tuple[ExperimentalToken, ...]


class ExperimentalTiming(StrictModel):
    label: Literal["experimental-token-timing"]
    token_level: tuple[SegmentTokenTiming, ...] | None
    consistent_with_segments: bool
    inconsistency: str | None = None

    @model_validator(mode="after")
    def require_flag_consistency(self) -> ExperimentalTiming:
        if self.consistent_with_segments != (self.inconsistency is None):
            raise PydanticCustomError(
                "experimental_flag_mismatch",
                "consistent_with_segments must agree with inconsistency",
            )
        return self


class AsrSettingsEcho(StrictModel):
    ffmpeg_argv: tuple[str, ...] = Field(min_length=1)
    whisper_argv: tuple[str, ...] = Field(min_length=1)

    @field_validator("ffmpeg_argv", "whisper_argv")
    @classmethod
    def require_absolute_local_binaries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not Path(value[0]).is_absolute():
            raise ValueError(f"argv[0] must be an absolute pinned binary, got {value[0]!r}")
        for token in value:
            if "://" in token or token in {"ffmpeg", "ffprobe", "whisper-cli"}:
                raise ValueError(f"argv must use absolute local binaries only, got {token!r}")
        return value


class TranscriptArtifact(ArtifactEnvelope[Literal["transcript_asr_whisper_cpp"]]):
    input_binding: AsrInputBinding
    segments: tuple[TranscriptSegment, ...] = Field(min_length=1)
    experimental: ExperimentalTiming
    settings_echo: AsrSettingsEcho

    @model_validator(mode="after")
    def require_content_hash_binding(self) -> TranscriptArtifact:
        expected = transcript_content_hash(
            self.input_binding, self.segments, self.experimental, self.settings_echo
        )
        if self.content_hash != expected:
            raise PydanticCustomError(
                "content_hash_mismatch", "content_hash must bind the canonical content bytes"
            )
        return self


def transcript_content_hash(
    input_binding: AsrInputBinding,
    segments: tuple[TranscriptSegment, ...],
    experimental: ExperimentalTiming,
    settings_echo: AsrSettingsEcho,
) -> str:
    payload = {
        "input_binding": input_binding.model_dump(mode="json"),
        "segments": [segment.model_dump(mode="json") for segment in segments],
        "experimental": experimental.model_dump(mode="json"),
        "settings_echo": settings_echo.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
