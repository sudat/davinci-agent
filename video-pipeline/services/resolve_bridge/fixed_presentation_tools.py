"""Pinned ffmpeg/ffprobe tool wrapper for the fixed-presentation spike."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from services.resolve_bridge.fixed_presentation_models import (
    FfprobeFormat,
    FfprobeReport,
    FfprobeStream,
)

FFMPEG_TIMEOUT_SECONDS: Final = 120


class RenderError(Exception):
    """The bounded render job or pinned ffmpeg call did not produce output."""


@dataclass(frozen=True, slots=True)
class SubtitlePacket:
    pts: float
    duration: float


@dataclass(frozen=True, slots=True)
class DecodeOutcome:
    argv: tuple[str, ...]
    exit_code: int
    stderr_tail: str


class MediaToolsApi(Protocol):
    def probe(self, path: Path) -> FfprobeReport: ...

    def subtitle_packets(self, path: Path) -> tuple[SubtitlePacket, ...]: ...

    def mux_subtitle(self, render_output: Path, srt_path: Path, paired: Path) -> None: ...

    def demux_subtitle(self, path: Path) -> bytes: ...

    def sha256(self, path: Path) -> str: ...

    def decode(self, path: Path) -> DecodeOutcome: ...


@dataclass(frozen=True, slots=True)
class MediaTools:
    ffmpeg_bin: Path
    ffprobe_bin: Path

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.ffmpeg_bin) if index == 0 else part for index, part in enumerate(argv)],
            check=False,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )

    def probe(self, path: Path) -> FfprobeReport:
        result = subprocess.run(
            [
                str(self.ffprobe_bin),
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        return _parse_probe(result.stdout)

    def subtitle_packets(self, path: Path) -> tuple[SubtitlePacket, ...]:
        result = subprocess.run(
            [
                str(self.ffprobe_bin),
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_packets",
                "-select_streams",
                "s",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
        packets = json.loads(result.stdout).get("packets", [])
        return tuple(
            SubtitlePacket(pts=float(p["pts_time"]), duration=float(p["duration_time"]))
            for p in packets
            if "pts_time" in p and "duration_time" in p
        )

    def mux_subtitle(self, render_output: Path, srt_path: Path, paired: Path) -> None:
        result = self._run(
            [
                "-nostdin",
                "-y",
                "-v",
                "error",
                "-i",
                str(render_output),
                "-i",
                str(srt_path),
                "-map",
                "0:v",
                "-map",
                "0:a",
                "-map",
                "1:0",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                "language=eng",
                str(paired),
            ]
        )
        if result.returncode != 0 or not paired.is_file():
            raise RenderError(f"ffmpeg mov_text mux failed: {result.stderr.strip()}")

    def demux_subtitle(self, path: Path) -> bytes:
        result = self._run(
            ["-nostdin", "-v", "error", "-i", str(path), "-map", "0:s:0", "-f", "srt", "-"]
        )
        if result.returncode != 0:
            raise RenderError(f"ffmpeg srt demux failed: {result.stderr.strip()}")
        return result.stdout.encode()

    def decode(self, path: Path) -> DecodeOutcome:
        argv = ["-nostdin", "-v", "error", "-i", str(path), "-f", "null", "-"]
        result = self._run(argv)
        return DecodeOutcome(
            argv=(str(self.ffmpeg_bin), *argv),
            exit_code=result.returncode,
            stderr_tail=result.stderr[-2000:],
        )

    def sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def _projected(model: type, raw: dict[str, object]) -> dict[str, object]:
    return {key: raw[key] for key in model.model_fields if key in raw}


def _parse_probe(payload: str) -> FfprobeReport:
    raw = json.loads(payload)
    streams = tuple(
        FfprobeStream.model_validate(_projected(FfprobeStream, stream)) for stream in raw["streams"]
    )
    fmt = FfprobeFormat.model_validate(_projected(FfprobeFormat, raw["format"]))
    return FfprobeReport(streams=streams, format=fmt)


