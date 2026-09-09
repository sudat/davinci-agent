"""Pinned-binary lock verification and bounded ffmpeg/ffprobe wrappers.

Both binary sha256s are verified against the frozen Phase-0C toolchain lock
before every subprocess run; drift is a hard refusal, never a warning.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final

from pydantic import BaseModel, BeforeValidator, ConfigDict

from services.foundation_io import sha256_file
from services.preview.models import (
    PreviewRenderError,
    PreviewToolchainError,
    PreviewVerificationError,
)
from services.toolchain.models import Phase0CToolchainLock, load_lock

PHASE_0C_LOCK: Final = Path("config/toolchains/phase-0c-v2.json")
FFMPEG_TIMEOUT_SECONDS: Final = 600
PROBE_TIMEOUT_SECONDS: Final = 120
HASH_PREFIX: Final = "SHA256="
SHA256_HEX_LENGTH: Final = 64


def _tuple[Value](value: list[Value] | tuple[Value, ...]) -> tuple[Value, ...]:
    return tuple(value)


type Sequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(_tuple)]


class _ProjectedModel(BaseModel):
    """ffprobe payloads carry many irrelevant keys; project only known fields."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class ProbeStream(_ProjectedModel):
    codec_type: str
    codec_name: str | None = None
    width: int | None = None
    height: int | None = None
    r_frame_rate: str | None = None
    avg_frame_rate: str | None = None
    duration: str | None = None
    nb_frames: str | None = None
    nb_read_frames: str | None = None
    sample_rate: str | None = None
    channels: int | None = None


class ProbeFormat(_ProjectedModel):
    duration: str | None = None


class ProbeReport(_ProjectedModel):
    streams: Sequence[ProbeStream] = ()
    format: ProbeFormat | None = None


@dataclass(frozen=True, slots=True)
class PinnedTools:
    ffmpeg: Path
    ffprobe: Path
    ffmpeg_sha256: str
    ffprobe_sha256: str

    def verify_current(self) -> None:
        """Re-hash both binaries against the frozen lock; refuse on drift."""

        for path, expected, name in (
            (self.ffmpeg, self.ffmpeg_sha256, "ffmpeg"),
            (self.ffprobe, self.ffprobe_sha256, "ffprobe"),
        ):
            if not path.is_file():
                raise PreviewToolchainError(f"pinned {name} binary missing: {path}")
            if sha256_file(path) != expected:
                raise PreviewToolchainError(
                    f"pinned {name} sha256 drift: {path} no longer matches the frozen lock"
                )


def load_pinned_tools(lock_path: Path = PHASE_0C_LOCK) -> PinnedTools:
    lock = load_lock(lock_path)
    if not isinstance(lock, Phase0CToolchainLock):
        raise PreviewToolchainError(f"not a phase-0c toolchain lock: {lock_path}")
    tools = PinnedTools(
        ffmpeg=Path(lock.ffmpeg.ffmpeg.path),
        ffprobe=Path(lock.ffmpeg.ffprobe.path),
        ffmpeg_sha256=lock.ffmpeg.ffmpeg.sha256,
        ffprobe_sha256=lock.ffmpeg.ffprobe.sha256,
    )
    tools.verify_current()
    return tools


def _capped(default_seconds: int, timeout_seconds: float | None) -> float:
    if timeout_seconds is None:
        return float(default_seconds)
    return min(float(default_seconds), timeout_seconds)


def run_bounded(
    argv: tuple[str, ...] | list[str],
    *,
    timeout_seconds: float = FFMPEG_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(part) for part in argv],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise PreviewRenderError(
            f"bounded command exceeded {timeout_seconds}s: {argv[0]}"
        ) from error


def _parse_probe(payload: str) -> ProbeReport:
    raw: object = json.loads(payload)
    report = ProbeReport.model_validate(raw if isinstance(raw, dict) else {})
    if report.format is None:
        raise PreviewVerificationError("ffprobe payload lacks format section")
    return report


def probe_file(
    tools: PinnedTools,
    path: Path,
    *,
    count_frames: bool = False,
    timeout_seconds: float | None = None,
) -> ProbeReport:
    tools.verify_current()
    argv = [
        str(tools.ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
    ]
    if count_frames:
        argv.append("-count_frames")
    argv.append(str(path))
    result = run_bounded(
        argv, timeout_seconds=_capped(PROBE_TIMEOUT_SECONDS, timeout_seconds)
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise PreviewVerificationError(
            f"ffprobe failed for {path}: exit={result.returncode} {result.stderr.strip()}"
        )
    return _parse_probe(result.stdout)


def decoded_video_sha256(
    tools: PinnedTools, path: Path, *, timeout_seconds: float | None = None
) -> str:
    """Full-decode semantic hash of the video stream (container bytes may differ)."""

    tools.verify_current()
    result = run_bounded(
        [
            str(tools.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ],
        timeout_seconds=_capped(PROBE_TIMEOUT_SECONDS, timeout_seconds),
    )
    if result.returncode != 0 or not result.stdout.strip().startswith(HASH_PREFIX):
        raise PreviewVerificationError(
            f"decoded video hash failed for {path}: {result.stderr.strip()}"
        )
    digest = result.stdout.strip().removeprefix(HASH_PREFIX).strip()
    if len(digest) != SHA256_HEX_LENGTH or any(char not in "0123456789abcdef" for char in digest):
        raise PreviewVerificationError(f"malformed decoded video hash output: {result.stdout!r}")
    return digest


def demux_subtitle(
    tools: PinnedTools, path: Path, *, timeout_seconds: float | None = None
) -> bytes:
    tools.verify_current()
    result = run_bounded(
        [
            str(tools.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:s:0",
            "-f",
            "srt",
            "-",
        ],
        timeout_seconds=_capped(PROBE_TIMEOUT_SECONDS, timeout_seconds),
    )
    if result.returncode != 0:
        raise PreviewVerificationError(f"subtitle demux failed for {path}: {result.stderr.strip()}")
    return result.stdout.encode()


__all__ = [
    "FFMPEG_TIMEOUT_SECONDS",
    "PHASE_0C_LOCK",
    "PinnedTools",
    "ProbeFormat",
    "ProbeReport",
    "ProbeStream",
    "decoded_video_sha256",
    "demux_subtitle",
    "load_pinned_tools",
    "probe_file",
    "run_bounded",
]
