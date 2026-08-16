from __future__ import annotations

import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from services.foundation_io import atomic_write

EXPECTED_FRAME_COUNT: Final = 30


class ProbeAssertionError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class ProbeStream(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    codec_type: str
    avg_frame_rate: str
    duration: str | None = None
    nb_frames: str | None = None
    nb_read_frames: str | None = None


class ProbeFormat(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    duration: str


class ProbePayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    streams: tuple[ProbeStream, ...]
    format: ProbeFormat


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    frame_rate: str
    frame_count: int
    duration_milliseconds: int


def assert_probe_payload(parsed: ProbePayload) -> ProbeResult:
    video = next((stream for stream in parsed.streams if stream.codec_type == "video"), None)
    if video is None:
        raise ProbeAssertionError("ffprobe payload has no video stream")
    frame_count_text = video.nb_read_frames or video.nb_frames
    if frame_count_text is None:
        raise ProbeAssertionError("ffprobe payload has no frame count")
    try:
        frame_count = int(frame_count_text)
        duration = Decimal(video.duration or parsed.format.duration)
    except (InvalidOperation, ValueError) as error:
        raise ProbeAssertionError("ffprobe duration or frame count is malformed") from error
    if (
        video.avg_frame_rate != "30/1"
        or frame_count != EXPECTED_FRAME_COUNT
        or duration != Decimal("1.000000")
    ):
        raise ProbeAssertionError("ffprobe fps, frame count, or duration assertion failed")
    return ProbeResult(
        frame_rate=video.avg_frame_rate,
        frame_count=frame_count,
        duration_milliseconds=1000,
    )


def run_ffmpeg_probe(ffmpeg: Path, ffprobe: Path, smoke_dir: Path) -> tuple[Path, ...]:
    smoke_dir.mkdir(parents=True, exist_ok=True)
    encoded = smoke_dir / "testsrc2-1s.mp4"
    decoded = smoke_dir / "testsrc2-decoded.mov"
    probe = smoke_dir / "testsrc2-ffprobe.json"
    subprocess.run(
        [
            str(ffmpeg), "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=320x240:rate=30", "-frames:v", "30", "-vf",
            "scale=320:240,fps=30,setpts=N/(30*TB)", "-r", "30", "-an",
            "-c:v", "h264_videotoolbox", "-y", str(encoded),
        ],
        check=True,
    )
    subprocess.run(
        [
            str(ffmpeg), "-v", "error", "-i", str(encoded), "-frames:v", "30",
            "-vf", "scale=320:240,fps=30,setpts=N/(30*TB)", "-r", "30",
            "-an", "-c:v", "h264_videotoolbox", "-y", str(decoded),
        ],
        check=True,
    )
    result = subprocess.run(
        [
            str(ffprobe), "-v", "error", "-select_streams", "v:0",
            "-count_frames", "-show_streams", "-show_format", "-of", "json",
            str(decoded),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        payload = ProbePayload.model_validate_json(result.stdout)
    except ValidationError as error:
        raise ProbeAssertionError(f"invalid ffprobe payload: {error}") from error
    assert_probe_payload(payload)
    atomic_write(probe, result.stdout.encode())
    return encoded, decoded, probe
