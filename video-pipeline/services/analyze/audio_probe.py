"""Audio decode validation for the Edit-Source wav (pinned ffprobe/ffmpeg).

The pinned binaries are hash-verified against the frozen Phase-1 toolchain
lock before any use. Validation has two stages, both bounded:

1. ffprobe stream facts — exactly one audio stream whose codec, sample rate,
   and channels must match the DECLARED facts. A sample-rate disagreement is
   the time-base confusion guard and refuses with
   ``analyze_time_base_mismatch``.
2. a bounded pinned-ffmpeg decode pass ``-nostdin -v error -i <wav> -f null
   -`` — a non-zero exit OR any stderr output at error level means the file
   does not decode cleanly (Todo-18 DecodeEvidence pattern) and is reported
   as ``corrupt_decode``.

All subprocess execution in the analyzer stack routes through
``run_bounded`` so tests can prove cache hits never re-execute tools.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.analyze.analysis_models import (
    AnalyzeError,
    AnalyzeRequestError,
    AnalyzeToolDriftError,
    AudioProbeArtifact,
    ProbeFailure,
    StreamFacts,
    refuse_edit_plan,
)
from services.toolchain.models import (
    LockError,
    Phase1TechnicalToolchainLock,
    load_lock,
)
from services.toolchain.verify import verify_binary

DEFAULT_LOCK_PATH: Final = Path("config/toolchains/phase-1-technical-v2.json")
DECODE_TIMEOUT_SEC: Final = 300
PROBE_TIMEOUT_SEC: Final = 120


def run_bounded(
    argv: tuple[str, ...], timeout_sec: int, what: str
) -> subprocess.CompletedProcess[str]:
    """The single subprocess entry point of the analyzer stack."""

    if not argv or not Path(argv[0]).is_absolute():
        raise AnalyzeRequestError(f"{what} argv[0] must be an absolute pinned binary: {argv!r}")
    try:
        return subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=timeout_sec
        )
    except subprocess.TimeoutExpired as error:
        raise AnalyzeError(f"{what} exceeded the bounded timeout of {timeout_sec}s") from error
    except OSError as error:
        raise AnalyzeError(f"{what} failed to execute: {error}") from error


@dataclass(frozen=True, slots=True)
class PinnedAudioTools:
    ffmpeg: Path
    ffprobe: Path
    ffmpeg_sha256: str
    ffprobe_sha256: str


def resolve_audio_tools(lock_path: Path = DEFAULT_LOCK_PATH) -> PinnedAudioTools:
    lock = load_lock(lock_path)
    if not isinstance(lock, Phase1TechnicalToolchainLock):
        raise AnalyzeToolDriftError(f"lock {lock_path} is not a phase-1-technical lock")
    try:
        verify_binary(lock.ffmpeg.ffmpeg)
        verify_binary(lock.ffmpeg.ffprobe)
    except LockError as error:
        raise AnalyzeToolDriftError(f"pinned binary hash drift: {error}") from error
    return PinnedAudioTools(
        ffmpeg=Path(lock.ffmpeg.ffmpeg.path),
        ffprobe=Path(lock.ffmpeg.ffprobe.path),
        ffmpeg_sha256=lock.ffmpeg.ffmpeg.sha256,
        ffprobe_sha256=lock.ffmpeg.ffprobe.sha256,
    )


def _probed_facts(ffprobe: Path, wav: Path) -> tuple[StreamFacts | None, ProbeFailure | None]:
    argv = (
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(wav),
    )
    result = run_bounded(argv, PROBE_TIMEOUT_SEC, "ffprobe stream facts")
    if result.returncode != 0:
        return None, ProbeFailure(
            code="probe_failed", detail=result.stderr.strip()[-500:] or "ffprobe failed"
        )
    try:
        payload = json.loads(result.stdout)
        streams = payload["streams"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        return None, ProbeFailure(code="probe_failed", detail=f"ffprobe payload malformed: {error}")
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(audio) != 1:
        return None, ProbeFailure(
            code="stream_facts_mismatch",
            detail=f"expected exactly one audio stream, found {len(audio)}",
        )
    stream = audio[0]
    if stream.get("codec_name") != "pcm_s16le" or stream.get("channels") != 1:
        return None, ProbeFailure(
            code="stream_facts_mismatch",
            detail=(
                f"expected pcm_s16le/1, got {stream.get('codec_name')}/{stream.get('channels')}"
            ),
        )
    rate_text = stream.get("sample_rate")
    try:
        rate = int(rate_text) if isinstance(rate_text, str) else -1
    except ValueError:
        rate = -1
    if rate not in (16000, 48000):
        return None, ProbeFailure(
            code="stream_facts_mismatch", detail=f"unsupported sample rate: {rate_text!r}"
        )
    return StreamFacts(sample_rate=rate, channels=1, codec="pcm_s16le"), None


def probe_audio(
    wav: Path,
    declared: StreamFacts,
    *,
    tools: PinnedAudioTools | None = None,
    lock_path: Path = DEFAULT_LOCK_PATH,
) -> AudioProbeArtifact:
    """Validate the wav: declared facts must match the probed stream, and the
    pinned ffmpeg must decode it cleanly. Never mutates anything."""

    refuse_edit_plan(declared)
    if not isinstance(declared, StreamFacts):
        raise AnalyzeRequestError("probe_audio requires declared StreamFacts")
    resolved = tools if tools is not None else resolve_audio_tools(lock_path)
    if not wav.is_file():
        return AudioProbeArtifact(
            decode_ok=False,
            failure=ProbeFailure(code="probe_failed", detail=f"wav missing: {wav}"),
            ffprobe_sha256=resolved.ffprobe_sha256,
            ffmpeg_sha256=resolved.ffmpeg_sha256,
        )

    facts, probe_failure = _probed_facts(resolved.ffprobe, wav)
    if probe_failure is not None or facts is None:
        failure = probe_failure if probe_failure is not None else ProbeFailure(
            code="probe_failed", detail="ffprobe returned no stream facts"
        )
        return AudioProbeArtifact(
            decode_ok=False,
            failure=failure,
            ffprobe_sha256=resolved.ffprobe_sha256,
            ffmpeg_sha256=resolved.ffmpeg_sha256,
        )
    if facts.sample_rate != declared.sample_rate:
        return AudioProbeArtifact(
            decode_ok=False,
            failure=ProbeFailure(
                code="time_base_mismatch",
                detail=(
                    f"declared {declared.sample_rate} Hz but the wav probes as "
                    f"{facts.sample_rate} Hz — refusing to mix time bases"
                ),
            ),
            ffprobe_sha256=resolved.ffprobe_sha256,
            ffmpeg_sha256=resolved.ffmpeg_sha256,
        )

    decode_argv = (
        str(resolved.ffmpeg),
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(wav),
        "-f",
        "null",
        "-",
    )
    decode = run_bounded(decode_argv, DECODE_TIMEOUT_SEC, "audio decode validation")
    if decode.returncode != 0 or decode.stderr.strip():
        return AudioProbeArtifact(
            decode_ok=False,
            failure=ProbeFailure(
                code="corrupt_decode",
                detail=decode.stderr.strip()[-500:]
                or f"ffmpeg decode exited {decode.returncode}",
            ),
            ffprobe_sha256=resolved.ffprobe_sha256,
            ffmpeg_sha256=resolved.ffmpeg_sha256,
            decode_argv=decode_argv,
        )
    return AudioProbeArtifact(
        decode_ok=True,
        facts=facts,
        ffprobe_sha256=resolved.ffprobe_sha256,
        ffmpeg_sha256=resolved.ffmpeg_sha256,
        decode_argv=decode_argv,
    )


__all__ = [
    "DEFAULT_LOCK_PATH",
    "AudioProbeArtifact",
    "PinnedAudioTools",
    "probe_audio",
    "resolve_audio_tools",
    "run_bounded",
]
