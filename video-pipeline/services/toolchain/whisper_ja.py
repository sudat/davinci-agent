"""Pinned local Japanese ASR toolchain section (whisper.cpp CLI) for Phase 1.

Freezes the CMake tool, the whisper.cpp source commit and build provenance,
the whisper-cli binary, the ggml large-v3-turbo model, the frozen FFmpeg
preprocessing argv, and the macOS TTS voice used to synthesize fixture speech.
The smoke synthesizes Japanese speech, transcribes it with the pinned stack,
and asserts the fed key phrase appears in the real transcript.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator

from services.contracts.primitives import StrictModel
from services.foundation_io import atomic_write, sha256_file
from services.toolchain.binaries import BinaryRecord  # noqa: TC001 (pydantic runtime)

type Argv = Annotated[tuple[str, ...], Field(min_length=1)]

PINNED_MODEL_SHA256 = "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"
PINNED_WHISPER_COMMIT = "1fe009caeda75f69bc864d6370b10674e45a92bd"


class WhisperSourceRecord(StrictModel):
    repo_url: Literal["https://github.com/ggml-org/whisper.cpp"]
    commit: Literal["1fe009caeda75f69bc864d6370b10674e45a92bd"]
    source_path: str

    @field_validator("source_path")
    @classmethod
    def require_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("whisper source path must be absolute")
        return value


class WhisperBuildRecord(StrictModel):
    cmake_configure_argv: Argv
    cmake_build_argv: Argv
    compiler: str
    build_type: Literal["Release"]


class WhisperModelRecord(StrictModel):
    repo: Literal["ggml-org/whisper-large-v3-turbo"]
    revision: Literal["5359861c739e955e79d9a303bcbc70fb988958b1"]
    # The pinned repo served 401 from this host; the byte-identical upstream
    # LFS object was fetched from ggerganov/whisper.cpp and admitted ONLY
    # because the sha256 below matches the pinned revision's required hash.
    served_via: Literal[
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
    ]
    path: str
    sha256: Literal["1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"]
    size_bytes: Literal[1624555275]

    @field_validator("path")
    @classmethod
    def require_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("model path must be absolute")
        return value


class TtsRecord(StrictModel):
    provider: Literal["macos-say"]
    voice: Literal["Kyoko"]
    language: Literal["ja_JP"]
    tool_path: Literal["/usr/bin/say"]


class WhisperSmokeProfile(StrictModel):
    profile_id: Literal["whisper-ja"]
    language: Literal["ja"]
    fed_text: str
    key_phrase: str
    preprocessing_argv: Argv
    whisper_cli_argv: Argv
    whisper_timeout_sec: Literal[600]


class WhisperJaSection(StrictModel):
    schema_version: Literal["whisper-ja-v1"]
    adapter: Literal["whisper-cpp-cli"]
    cmake: BinaryRecord
    source: WhisperSourceRecord
    build: WhisperBuildRecord
    whisper_cli: BinaryRecord
    model: WhisperModelRecord
    tts: TtsRecord
    smoke: WhisperSmokeProfile


class WhisperSmokeError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def _normalize_japanese(text: str) -> str:
    return re.sub(r"[、。.,!?\s\u3000「」『』]", "", text)


def _substitute(argv: tuple[str, ...], mapping: dict[str, str]) -> list[str]:
    return [mapping.get(token, token) for token in argv]


def _require_placeholders(argv: tuple[str, ...], placeholders: tuple[str, ...]) -> None:
    joined = "\n".join(argv)
    for placeholder in placeholders:
        if placeholder not in joined:
            msg = f"whisper smoke argv is missing {placeholder}"
            raise WhisperSmokeError(msg)


def run_whisper_smoke(
    ffmpeg_path: str,
    section: WhisperJaSection,
    smoke_dir: Path,
) -> tuple[Path, ...]:
    """say -> aiff -> pinned ffmpeg -> wav -> whisper-cli; assert the fed phrase."""

    smoke_dir.mkdir(parents=True, exist_ok=True)
    profile = section.smoke
    _require_placeholders(profile.preprocessing_argv, ("{input}", "{output}"))
    _require_placeholders(
        profile.whisper_cli_argv, ("{whisper_cli}", "{model}", "{wav}", "{out_base}")
    )
    aiff = smoke_dir / "smoke-ja.aiff"
    wav = smoke_dir / "smoke-ja.wav"
    out_base = smoke_dir / "smoke-ja"
    transcript_json = smoke_dir / "smoke-ja.json"
    request_record = smoke_dir / "smoke-request.json"
    subprocess.run(
        [section.tts.tool_path, "-v", section.tts.voice, "-o", str(aiff), profile.fed_text],
        check=True,
        timeout=120,
        capture_output=True,
        text=True,
    )
    preprocess = _substitute(
        profile.preprocessing_argv,
        {"{ffmpeg}": ffmpeg_path, "{input}": str(aiff), "{output}": str(wav)},
    )
    subprocess.run([ffmpeg_path, *preprocess], check=True, timeout=120, capture_output=True)
    cli = _substitute(
        profile.whisper_cli_argv,
        {
            "{whisper_cli}": section.whisper_cli.path,
            "{model}": section.model.path,
            "{wav}": str(wav),
            "{out_base}": str(out_base),
        },
    )
    subprocess.run(
        cli, check=True, timeout=profile.whisper_timeout_sec, capture_output=True, text=True
    )
    payload = json.loads(transcript_json.read_text(encoding="utf-8"))
    segments = payload.get("transcription")
    if not isinstance(segments, list):
        raise WhisperSmokeError("whisper-cli JSON output has no transcription list")
    text = "".join(str(entry.get("text", "")) for entry in segments if isinstance(entry, dict))
    if _normalize_japanese(profile.key_phrase) not in _normalize_japanese(text):
        raise WhisperSmokeError(f"whisper transcript does not contain the fed key phrase: {text!r}")
    record = {
        "fed_text": profile.fed_text,
        "key_phrase": profile.key_phrase,
        "tts_voice": section.tts.voice,
        "preprocessing_argv": list(profile.preprocessing_argv),
        "whisper_cli_argv": list(profile.whisper_cli_argv),
        "aiff_sha256": sha256_file(aiff),
        "wav_sha256": sha256_file(wav),
        "transcript_sha256": sha256_file(transcript_json),
        "transcript_text": text,
    }
    atomic_write(
        request_record,
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
    )
    return aiff, wav, transcript_json, request_record


def verify_whisper_provenance(section: WhisperJaSection) -> None:
    """Hash-bind the cmake tool, whisper-cli binary, and the pinned model."""

    from services.toolchain.verify import (  # noqa: PLC0415 (cycle: verify imports this module)
        verify_binary,
    )

    if not Path(section.source.source_path).is_dir():
        raise WhisperSmokeError("whisper.cpp source checkout is missing")
    verify_binary(section.cmake)
    verify_binary(section.whisper_cli)
    model = Path(section.model.path)
    if (
        not model.is_file()
        or sha256_file(model) != PINNED_MODEL_SHA256
        or model.stat().st_size != section.model.size_bytes
    ):
        raise WhisperSmokeError("pinned whisper model hash or size drift")
