"""Pinned-ffmpeg decode/extract/pixel-metric operations."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from PIL import Image, ImageChops

from services.preview.models import PreviewToolchainError

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools

DECODE_TIMEOUT_SECONDS: Final = 1800
EXTRACT_TIMEOUT_SECONDS: Final = 1800
DEFAULT_DIFF_THRESHOLD: Final = 8


class ChapterCardMediaError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class FrameGeometry:
    """Decoded rgb24 frame dimensions shared by every extraction seam."""

    width: int
    height: int

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * 3


@dataclass(frozen=True, slots=True)
class FrameWindow:
    """A contiguous span of frames to decode in one pinned pass."""

    geometry: FrameGeometry
    start_frame: int
    count: int


@dataclass(frozen=True, slots=True)
class FrameDiff:
    max_abs: int
    mean_abs: float
    mismatched_fraction: float


def run_pinned(tools: PinnedTools, argv: tuple[str, ...], *, timeout: int) -> bytes:
    """Verify both binary pins immediately before the subprocess call."""

    try:
        tools.verify_current()
    except PreviewToolchainError as error:
        raise ChapterCardMediaError("toolchain-pin-drift", str(error)) from error
    try:
        result = subprocess.run(argv, check=False, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise ChapterCardMediaError(
            "command-timeout", f"pinned ffmpeg exceeded {timeout}s: {argv[0]}"
        ) from error
    except OSError as error:
        raise ChapterCardMediaError(
            "command-launch-failed", f"cannot run {argv[0]}: {error}"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace")[-500:]
        raise ChapterCardMediaError(
            "command-failed", f"{argv[0]} exited {result.returncode}: {detail}"
        )
    return result.stdout


def decode_pcm_s16le(tools: PinnedTools, media: Path) -> bytes:
    """Decode the first audio stream to canonical s16le stereo bytes."""

    if not media.is_file():
        raise ChapterCardMediaError("media-not-found", f"media file missing: {media}")
    return run_pinned(
        tools,
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(media),
            "-map",
            "0:a:0",
            "-f",
            "s16le",
            "-c:a",
            "pcm_s16le",
            "-",
        ),
        timeout=DECODE_TIMEOUT_SECONDS,
    )


def _select_expression(indices: tuple[int, ...]) -> str:
    return "+".join(f"eq(n\\,{index})" for index in indices)


def extract_frames(tools: PinnedTools, media: Path, window: FrameWindow) -> bytes:
    """Extract the window's contiguous decoded frames as raw rgb24 in one pass."""

    last = window.start_frame + window.count - 1
    frames = run_pinned(
        tools,
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(media),
            # passthrough is mandatory: default vsync duplicates the final
            # selected frame to fill the source duration after select drops
            "-fps_mode",
            "passthrough",
            "-vf",
            f"select='between(n\\,{window.start_frame}\\,{last})',setpts=PTS-STARTPTS",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ),
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    expected = window.count * window.geometry.frame_bytes
    if len(frames) != expected:
        raise ChapterCardMediaError(
            "extract-length",
            f"extracted {len(frames)} bytes from frame {window.start_frame}, "
            f"expected {expected}",
        )
    return frames


def extract_selected_frames(
    tools: PinnedTools, media: Path, geometry: FrameGeometry, frames: tuple[int, ...]
) -> dict[int, bytes]:
    """Decode exactly the requested frames in one pinned pass per media file.

    ``-frames:v`` terminates the decode as soon as the last selection is
    emitted, so sparse sampling never scans the whole stream.
    """

    if not frames:
        return {}
    ordered = tuple(sorted(set(frames)))
    payload = run_pinned(
        tools,
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(media),
            "-fps_mode",
            "passthrough",
            "-vf",
            f"select='{_select_expression(ordered)}',setpts=PTS-STARTPTS",
            "-frames:v",
            str(len(ordered)),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ),
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    frame_bytes = geometry.frame_bytes
    expected = len(ordered) * frame_bytes
    if len(payload) != expected:
        raise ChapterCardMediaError(
            "extract-length",
            f"selected {len(ordered)} frames from {media} but got "
            f"{len(payload)} bytes, expected {expected}",
        )
    return {
        index: payload[position * frame_bytes : (position + 1) * frame_bytes]
        for position, index in enumerate(ordered)
    }


def dump_frames_raw(
    tools: PinnedTools, media: Path, window: FrameWindow, destination: Path
) -> None:
    """One pinned decode writing `count` raw rgb24 frames straight to a file.

    Streaming to disk keeps bounded RAM for spans too large to hold as one
    bytes object (45 full-HD frames is ~267 MiB).
    """

    last = window.start_frame + window.count - 1
    run_pinned(
        tools,
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(media),
            "-fps_mode",
            "passthrough",
            "-vf",
            f"select='between(n\\,{window.start_frame}\\,{last})',setpts=PTS-STARTPTS",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            str(destination),
        ),
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    expected = window.count * window.geometry.frame_bytes
    try:
        size = destination.stat().st_size
    except OSError as error:
        raise ChapterCardMediaError(
            "extract-length", f"cannot stat raw span file {destination}: {error}"
        ) from error
    if size != expected:
        raise ChapterCardMediaError(
            "extract-length",
            f"raw span file {destination} holds {size} bytes, expected {expected}",
        )


def frame_diff(
    a: bytes | memoryview, b: bytes | memoryview, geometry: FrameGeometry,
    *, threshold: int = DEFAULT_DIFF_THRESHOLD,
) -> FrameDiff:
    """Pixel metrics between two rgb24 frames of identical geometry."""

    width, height = geometry.width, geometry.height
    if len(a) != geometry.frame_bytes or len(b) != len(a):
        raise ChapterCardMediaError(
            "diff-geometry", f"frames are {len(a)}/{len(b)} bytes for {width}x{height}"
        )
    left = Image.frombytes("RGB", (width, height), a)
    right = Image.frombytes("RGB", (width, height), b)
    diff = ImageChops.difference(left, right)

    def _band_peak(histogram: list[int]) -> int:
        return max((index for index, count in enumerate(histogram) if count), default=0)

    bands = [band.histogram() for band in diff.split()]
    max_abs = max(_band_peak(histogram) for histogram in bands)
    values = width * height * 3
    channel_sum = sum(
        index * count for histogram in bands for index, count in enumerate(histogram)
    )
    mean_abs = channel_sum / values

    def _over(value: int) -> int:
        return 255 if value > threshold else 0

    red, green, blue = (band.point(_over) for band in diff.split())
    combined = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    mismatched = combined.histogram()[255]
    pixels = width * height
    return FrameDiff(
        max_abs=max_abs, mean_abs=mean_abs, mismatched_fraction=mismatched / pixels
    )


__all__ = [
    "ChapterCardMediaError",
    "FrameDiff",
    "FrameGeometry",
    "FrameWindow",
    "decode_pcm_s16le",
    "dump_frames_raw",
    "extract_frames",
    "extract_selected_frames",
    "frame_diff",
    "run_pinned",
]
