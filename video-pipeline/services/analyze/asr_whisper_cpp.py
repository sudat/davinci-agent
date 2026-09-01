"""Pinned local Japanese ASR adapter driven by the whisper.cpp CLI.

Execution contract (refusal is always before any execution on drift):
- absolute, hash-verified binaries only: ffmpeg, ffprobe, whisper-cli, and
  the ggml model are checked against the frozen Phase-1 toolchain lock;
- the input is converted with the frozen preprocessing argv to mono 16 kHz
  PCM-s16 WAV, probed (16000/1/pcm_s16le), sha256-recorded, and bound into
  the transcript artifact;
- whisper-cli runs with the frozen flag set (see asr_models); token timing
  is parsed only as labeled experimental evidence;
- no audio ever leaves the host: the pinned ffmpeg is built --disable-network
  and every invoked binary is a local absolute path;
- the pinned build is GGML_METAL=OFF (CPU-only by lock) and the frozen argv
  contains no accelerator-force flag; CPU and temperature fallback stay
  internal to whisper.cpp.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import ValidationError

from services.analyze.asr_cache import AsrCache, CacheBinding, cache_key
from services.analyze.asr_models import (
    FORBIDDEN_ACCELERATOR_FLAGS,
    FROZEN_PREPROCESSING_ARGV,
    FROZEN_TEMPERATURE,
    FROZEN_THREADS,
    FROZEN_WHISPER_ARGV_TEMPLATE,
    AsrCacheConflictError,
    AsrError,
    AsrExecutionError,
    AsrInputBinding,
    AsrRequest,
    AsrRequestError,
    AsrSettingsEcho,
    AsrToolDriftError,
    AsrWavProbeError,
    TranscriptArtifact,
    WavProbeSummary,
)
from services.analyze.asr_parse import build_transcript_artifact, parse_whisper_payload
from services.contracts.primitives import StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.ingest.probe import ProbeError, probe_media, stream_entries
from services.toolchain.models import (
    LockError,
    Phase1TechnicalToolchainLock,
    load_lock,
)
from services.toolchain.verify import verify_binary
from services.toolchain.whisper_ja import _require_placeholders, _substitute

DEFAULT_LOCK_PATH: Final = Path("config/toolchains/phase-1-technical-v2.json")
FFMPEG_TIMEOUT_SEC: Final = 300
WHISPER_TIMEOUT_SEC: Final = 600
WAV_NAME: Final = "asr-16k.wav"
OUT_BASE_NAME: Final = "whisper-out"
ARTIFACT_NAME: Final = "transcript-artifact.json"
RAW_EVIDENCE_NAME: Final = "whisper-cli-output.json"


class AsrTranscribeResult(StrictModel):
    artifact: TranscriptArtifact
    artifact_path: str
    raw_evidence_path: str
    cache_status: Literal["hit", "miss"]


@dataclass(frozen=True, slots=True)
class ResolvedPins:
    ffmpeg: Path
    ffprobe: Path
    cli: Path
    model: Path
    preprocessing_argv: tuple[str, ...]


def _resolve_pins(request: AsrRequest, lock_path: Path) -> ResolvedPins:
    lock = load_lock(lock_path)
    if not isinstance(lock, Phase1TechnicalToolchainLock):
        raise AsrToolDriftError(f"lock {lock_path} is not a phase-1-technical lock")
    section = lock.whisper_ja
    try:
        verify_binary(lock.ffmpeg.ffmpeg)
        verify_binary(lock.ffmpeg.ffprobe)
        verify_binary(section.whisper_cli)
    except LockError as error:
        raise AsrToolDriftError(f"pinned binary hash drift: {error}") from error
    model = Path(section.model.path)
    if (
        not model.is_file()
        or sha256_file(model) != section.model.sha256
        or model.stat().st_size != section.model.size_bytes
    ):
        raise AsrToolDriftError(f"pinned whisper model hash or size drift: {model}")
    requested_pins = (
        (request.ffmpeg.path, request.ffmpeg.sha256, lock.ffmpeg.ffmpeg.path,
         lock.ffmpeg.ffmpeg.sha256, "ffmpeg"),
        (request.cli.path, request.cli.sha256, section.whisper_cli.path,
         section.whisper_cli.sha256, "whisper-cli"),
        (request.model.path, request.model.sha256, section.model.path,
         section.model.sha256, "whisper model"),
    )
    for requested_path, requested_sha, locked_path, locked_sha, name in requested_pins:
        if requested_path != locked_path or requested_sha != locked_sha:
            raise AsrToolDriftError(
                f"request {name} pin does not match the frozen lock: {requested_path}"
            )
    if section.smoke.preprocessing_argv != FROZEN_PREPROCESSING_ARGV:
        raise AsrToolDriftError("frozen preprocessing argv drift in the toolchain lock")
    if FORBIDDEN_ACCELERATOR_FLAGS.intersection(FROZEN_WHISPER_ARGV_TEMPLATE):
        raise AsrRequestError("frozen whisper argv contains a forbidden accelerator flag")
    return ResolvedPins(
        ffmpeg=Path(lock.ffmpeg.ffmpeg.path),
        ffprobe=Path(lock.ffmpeg.ffprobe.path),
        cli=Path(section.whisper_cli.path),
        model=model,
        preprocessing_argv=section.smoke.preprocessing_argv,
    )


def _verify_settings(request: AsrRequest) -> None:
    if request.temperature != FROZEN_TEMPERATURE or request.threads != FROZEN_THREADS:
        raise AsrRequestError(
            "request settings drift from the frozen whisper profile "
            f"(temperature={request.temperature}, threads={request.threads})"
        )


def _run(argv: tuple[str, ...], timeout_sec: int, what: str) -> subprocess.CompletedProcess[str]:
    if not Path(argv[0]).is_absolute():
        raise AsrRequestError(f"{what} argv[0] must be an absolute pinned binary: {argv[0]!r}")
    try:
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=timeout_sec
        )
    except subprocess.TimeoutExpired as error:
        raise AsrExecutionError(f"{what} exceeded the bounded timeout of {timeout_sec}s") from error
    except OSError as error:
        raise AsrExecutionError(f"{what} failed to execute: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip())[-800:]
        raise AsrExecutionError(f"{what} exited {result.returncode}: {detail}")
    return result


def _probe_wav(ffprobe: Path, wav: Path) -> None:
    try:
        streams = stream_entries(probe_media(ffprobe, wav))
    except ProbeError as error:
        raise AsrExecutionError(f"ffprobe of the preprocessed wav failed: {error}") from error
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(audio) != 1:
        raise AsrWavProbeError(f"expected exactly one audio stream, found {len(audio)}")
    stream = audio[0]
    if (
        stream.get("sample_rate") != "16000"
        or stream.get("channels") != 1
        or stream.get("codec_name") != "pcm_s16le"
    ):
        raise AsrWavProbeError(
            "preprocessed wav must be 16000 Hz / 1 channel / pcm_s16le, got "
            f"{stream.get('sample_rate')}/{stream.get('channels')}/{stream.get('codec_name')}"
        )


def transcribe(
    request: AsrRequest,
    *,
    work_dir: Path,
    cache_dir: Path,
    lock_path: Path = DEFAULT_LOCK_PATH,
) -> AsrTranscribeResult:
    _verify_settings(request)
    pins = _resolve_pins(request, lock_path)
    media = Path(request.input_media_path)
    if not media.is_file() or sha256_file(media) != request.input_media_sha256:
        raise AsrRequestError(f"input media missing or hash drift: {media}")

    work_dir.mkdir(parents=True, exist_ok=True)
    wav = (work_dir / WAV_NAME).resolve()
    _require_placeholders(pins.preprocessing_argv, ("{input}", "{output}"))
    ffmpeg_argv = (
        str(pins.ffmpeg),
        *_substitute(
            pins.preprocessing_argv,
            {"{ffmpeg}": str(pins.ffmpeg), "{input}": str(media), "{output}": str(wav)},
        ),
    )
    _run(tuple(ffmpeg_argv), FFMPEG_TIMEOUT_SEC, "ffmpeg preprocessing")
    _probe_wav(pins.ffprobe, wav)
    wav_sha256 = sha256_file(wav)

    out_base = (work_dir / OUT_BASE_NAME).resolve()
    whisper_argv = tuple(
        _substitute(
            FROZEN_WHISPER_ARGV_TEMPLATE,
            {
                "{whisper_cli}": str(pins.cli),
                "{model}": str(pins.model),
                "{wav}": str(wav),
                "{out_base}": str(out_base),
            },
        )
    )
    echo = AsrSettingsEcho(ffmpeg_argv=ffmpeg_argv, whisper_argv=whisper_argv)

    binding = CacheBinding(
        model_sha256=request.model.sha256,
        cli_sha256=request.cli.sha256,
        wav_sha256=wav_sha256,
        whisper_argv_template=FROZEN_WHISPER_ARGV_TEMPLATE,
        media_sha256=request.input_media_sha256,
        media_path=str(media),
    )
    key = cache_key(
        request.model.sha256, request.cli.sha256, wav_sha256, FROZEN_WHISPER_ARGV_TEMPLATE
    )
    cache = AsrCache(cache_dir)
    cached = cache.lookup(key, binding)

    if cached is not None:
        try:
            artifact = TranscriptArtifact.model_validate_json(cached.artifact_bytes)
        except ValidationError as error:
            raise AsrCacheConflictError(
                f"cache entry at {cache.entry_dir(key)} fails the artifact contract; "
                f"overwrite refused: {error}"
            ) from error
        artifact_bytes = cached.artifact_bytes
        raw_bytes = cached.raw_evidence_bytes
        cache_status: Literal["hit", "miss"] = "hit"
    else:
        _run(whisper_argv, WHISPER_TIMEOUT_SEC, "whisper-cli")
        raw_path = Path(f"{out_base}.json")
        try:
            raw_bytes = raw_path.read_bytes()
        except OSError as error:
            raise AsrExecutionError(
                f"whisper-cli did not produce the expected JSON output: {raw_path}"
            ) from error
        segments, token_groups = parse_whisper_payload(raw_bytes)
        artifact = build_transcript_artifact(
            binding=AsrInputBinding(
                media_path=str(media),
                media_sha256=request.input_media_sha256,
                wav_sha256=wav_sha256,
                wav_probe=WavProbeSummary(),
            ),
            segments=segments,
            token_groups=token_groups,
            settings_echo=echo,
        )
        artifact_bytes = canonical_model_bytes(artifact)
        cache.store(key, binding, artifact_bytes, raw_bytes)
        cache_status = "miss"

    artifact_path = work_dir / ARTIFACT_NAME
    raw_evidence_path = work_dir / RAW_EVIDENCE_NAME
    atomic_write(artifact_path, artifact_bytes)
    atomic_write(raw_evidence_path, raw_bytes)
    return AsrTranscribeResult(
        artifact=artifact,
        artifact_path=str(artifact_path),
        raw_evidence_path=str(raw_evidence_path),
        cache_status=cache_status,
    )


__all__ = ["AsrError", "AsrTranscribeResult", "transcribe"]
