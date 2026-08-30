"""Pinned, hash-verified ffmpeg/ffprobe tooling for the QC engine.

Binaries are resolved from the frozen phase-2 toolchain lock and re-hashed
before every use. Every subprocess call is bounded, runs with an absolute
pinned argv, and its raw stdout/stderr IS the evidence that checks attach
to issues.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.foundation_io import sha256_file
from services.resolve_bridge.fixed_presentation_models import (
    FfprobeFormat,
    FfprobeReport,
    FfprobeStream,
)
from services.toolchain.models import Phase2ToolchainLock, load_lock
from services.toolchain.verify import verify_binary

PHASE2_LOCK: Final = Path("config/toolchains/phase-2-v1.json")
PROBE_TIMEOUT_SEC: Final = 120
DECODE_TIMEOUT_SEC: Final = 300
MEASURE_TIMEOUT_SEC: Final = 300


class QcToolError(Exception):
    """A pinned tool invocation failed or produced malformed output."""


@dataclass(frozen=True, slots=True)
class QcTools:
    ffmpeg: Path
    ffprobe: Path
    ffmpeg_sha256: str
    ffprobe_sha256: str

    def verify_current(self) -> None:
        for path, expected, name in (
            (self.ffmpeg, self.ffmpeg_sha256, "ffmpeg"),
            (self.ffprobe, self.ffprobe_sha256, "ffprobe"),
        ):
            if not path.is_file() or sha256_file(path) != expected:
                raise QcToolError(f"pinned {name} binary drift: {path}")

    def run(
        self, argv: tuple[str, ...], timeout_sec: int, what: str
    ) -> subprocess.CompletedProcess[str]:
        if not argv or not Path(argv[0]).is_absolute():
            raise QcToolError(f"{what}: argv[0] must be an absolute pinned binary")
        self.verify_current()
        try:
            return subprocess.run(
                [str(part) for part in argv],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as error:
            raise QcToolError(
                f"{what} exceeded the bounded timeout of {timeout_sec}s"
            ) from error

    def probe_raw(self, render: Path) -> str:
        argv = (
            str(self.ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(render),
        )
        result = self.run(argv, PROBE_TIMEOUT_SEC, "ffprobe render metadata")
        if result.returncode != 0 or not result.stdout.strip():
            raise QcToolError(
                f"ffprobe failed on {render}: exit={result.returncode} "
                f"{result.stderr.strip()}"
            )
        return result.stdout

    def decode(self, render: Path) -> tuple[int, str]:
        argv = (
            str(self.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(render),
            "-f",
            "null",
            "-",
        )
        result = self.run(argv, DECODE_TIMEOUT_SEC, "full decode pass")
        return result.returncode, result.stderr.strip()[-500:]

    def demux_subtitle(self, render: Path) -> bytes:
        argv = (
            str(self.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(render),
            "-map",
            "0:s:0",
            "-f",
            "srt",
            "-",
        )
        result = self.run(argv, PROBE_TIMEOUT_SEC, "subtitle demux")
        if result.returncode != 0:
            raise QcToolError(f"subtitle demux failed: {result.stderr.strip()[-300:]}")
        return result.stdout.encode()

    def mono_wav(self, render: Path, out_wav: Path) -> None:
        argv = (
            str(self.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(render),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            str(out_wav),
        )
        result = self.run(argv, MEASURE_TIMEOUT_SEC, "mono wav decode")
        if result.returncode != 0:
            raise QcToolError(f"mono wav decode failed: {result.stderr.strip()[-300:]}")

    def frame_gray(self, media: Path, at_seconds: float, size: tuple[int, int]) -> bytes:
        """One bounded grayscale frame as raw WxH bytes for orientation NCC."""
        self.verify_current()
        width, height = size
        argv = (
            str(self.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{at_seconds:.3f}",
            "-i",
            str(media),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:{height}",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "-",
        )
        try:
            result = subprocess.run(
                [str(part) for part in argv],
                check=False,
                capture_output=True,
                timeout=PROBE_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as error:
            raise QcToolError(
                f"frame extraction exceeded the bounded timeout of {PROBE_TIMEOUT_SEC}s"
            ) from error
        if result.returncode != 0 or len(result.stdout) < width * height:
            raise QcToolError(
                f"frame extraction failed on {media}: exit={result.returncode} "
                f"{result.stderr.decode('utf-8', 'replace').strip()[-200:]}"
            )
        return result.stdout[: width * height]


def parse_probe_report(raw: str) -> tuple[FfprobeReport, list[dict[str, object]]]:
    try:
        payload = json.loads(raw)
        streams_raw = payload["streams"]
        streams = tuple(
            FfprobeStream.model_validate(
                {key: stream[key] for key in FfprobeStream.model_fields if key in stream}
            )
            for stream in streams_raw
        )
        fmt_payload = payload["format"]
        fmt = FfprobeFormat.model_validate(
            {key: fmt_payload[key] for key in FfprobeFormat.model_fields if key in fmt_payload}
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise QcToolError(f"ffprobe payload malformed: {error}") from error
    return FfprobeReport(streams=streams, format=fmt), streams_raw


def load_qc_tools(lock_path: Path = PHASE2_LOCK) -> QcTools:
    lock = load_lock(lock_path)
    if not isinstance(lock, Phase2ToolchainLock):
        raise QcToolError(f"not a phase-2 toolchain lock: {lock_path}")
    verify_binary(lock.ffmpeg.ffmpeg)
    verify_binary(lock.ffmpeg.ffprobe)
    return QcTools(
        ffmpeg=Path(lock.ffmpeg.ffmpeg.path),
        ffprobe=Path(lock.ffmpeg.ffprobe.path),
        ffmpeg_sha256=lock.ffmpeg.ffmpeg.sha256,
        ffprobe_sha256=lock.ffmpeg.ffprobe.sha256,
    )


__all__ = [
    "PHASE2_LOCK",
    "QcToolError",
    "QcTools",
    "load_qc_tools",
    "parse_probe_report",
]
