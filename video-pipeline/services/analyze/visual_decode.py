"""Bounded pinned-ffmpeg raw-luma extraction for the visual checks.

The pinned ffmpeg/ffprobe are hash-verified against the frozen Phase-1
toolchain lock before any use (reusing the Todo-34 resolver). Decode is
bounded twice: by a frame-count budget checked BEFORE any decode, and by a
hard subprocess timeout. Frames come back as raw 8-bit gray bytes
(``-vf scale=W:H,format=gray -f rawvideo``) parsed with the Python stdlib
only — no numpy, no cv2 — and every frame carries its index, its exact PTS
as a reduced rational in stream time_base units, and the source binding.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from services.analyze.analysis_models import AnalyzeRequestError
from services.analyze.audio_probe import PinnedAudioTools, resolve_audio_tools, run_bounded
from services.analyze.visual_constants import (
    DECODE_FILTER,
    DECODE_H,
    DECODE_TIMEOUT_SEC,
    DECODE_W,
    MAX_DECODE_FRAMES,
    PROBE_TIMEOUT_SEC,
)
from services.analyze.visual_models import (
    DecodeBinding,
    Pts,
    VisualDecodeError,
    VisualStreamFacts,
)


@dataclass(frozen=True, slots=True)
class LumaFrame:
    frame_index: int
    pts: Pts
    luma: bytes


def ensure_decode_budget(frame_count: int) -> None:
    """Refuse oversized decodes before spending any tool time."""

    if frame_count > MAX_DECODE_FRAMES:
        raise AnalyzeRequestError(
            f"decode budget exceeded: {frame_count} frames > frozen max {MAX_DECODE_FRAMES}"
        )


def _as_int(value: object) -> int | None:
    # ffprobe JSON emits some numeric fields (nb_frames) as strings
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _rational_parts(value: object, what: str) -> tuple[int, int]:
    if not isinstance(value, str) or "/" not in value:
        raise AnalyzeRequestError(f"stream {what} is not a rational: {value!r}")
    num_text, den_text = value.split("/", 1)
    try:
        num, den = int(num_text), int(den_text)
    except ValueError as error:
        raise AnalyzeRequestError(f"stream {what} is not a rational: {value!r}") from error
    if num <= 0 or den <= 0:
        raise AnalyzeRequestError(f"stream {what} is non-positive: {value!r}")
    return num, den


def probe_video_facts(ffprobe: Path, media: Path) -> VisualStreamFacts:
    argv = (
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(media),
    )
    result = run_bounded(argv, PROBE_TIMEOUT_SEC, "ffprobe video facts")
    if result.returncode != 0:
        raise VisualDecodeError(
            result.stderr.strip()[-500:] or f"ffprobe failed with exit {result.returncode}"
        )
    try:
        streams = json.loads(result.stdout)["streams"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise VisualDecodeError(f"ffprobe payload malformed: {error}") from error
    video = [stream for stream in streams if isinstance(stream, dict)]
    video = [stream for stream in video if stream.get("codec_type") == "video"]
    if len(video) != 1:
        raise AnalyzeRequestError(
            f"expected exactly one video stream, found {len(video)} in {media}"
        )
    stream = video[0]
    width, height = _as_int(stream.get("width")), _as_int(stream.get("height"))
    nb_frames = _as_int(stream.get("nb_frames"))
    if width is None or height is None or width <= 0 or height <= 0:
        raise AnalyzeRequestError(f"stream dimensions missing in {media}")
    if nb_frames is None or nb_frames <= 0:
        raise AnalyzeRequestError(f"stream nb_frames missing in {media}")
    rate_num, rate_den = _rational_parts(stream.get("r_frame_rate"), "frame rate")
    tb_num, tb_den = _rational_parts(stream.get("time_base"), "time base")
    return VisualStreamFacts(
        width=width,
        height=height,
        rate_num=rate_num,
        rate_den=rate_den,
        time_base_num=tb_num,
        time_base_den=tb_den,
        frame_count=nb_frames,
    )


def frame_pts(facts: VisualStreamFacts, frame_index: int) -> Pts:
    """Exact CFR frame PTS in stream time_base units, gcd-reduced."""

    seconds = Fraction(frame_index * facts.rate_den, facts.rate_num)
    in_time_base = seconds / Fraction(facts.time_base_num, facts.time_base_den)
    return Pts(num=in_time_base.numerator, den=in_time_base.denominator)


def _run_bounded_bytes(
    argv: tuple[str, ...], timeout_sec: int, what: str
) -> subprocess.CompletedProcess[bytes]:
    """Bounded subprocess with BINARY stdout (raw video bytes must not pass
    through a text decoder). Same pinned-absolute-argv discipline as
    ``run_bounded``."""

    if not argv or not Path(argv[0]).is_absolute():
        raise AnalyzeRequestError(f"{what} argv[0] must be an absolute pinned binary: {argv!r}")
    try:
        return subprocess.run(
            argv, check=False, capture_output=True, timeout=timeout_sec
        )
    except subprocess.TimeoutExpired as error:
        raise AnalyzeRequestError(
            f"{what} exceeded the bounded timeout of {timeout_sec}s"
        ) from error
    except OSError as error:
        raise AnalyzeRequestError(f"{what} failed to execute: {error}") from error


def decode_luma_frames(
    media: Path,
    facts: VisualStreamFacts,
    *,
    tools: PinnedAudioTools,
) -> tuple[LumaFrame, ...]:
    """Decode every frame to frozen-size raw gray via the pinned ffmpeg."""

    ensure_decode_budget(facts.frame_count)
    argv = (
        str(tools.ffmpeg),
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(media),
        "-vf",
        DECODE_FILTER,
        "-f",
        "rawvideo",
        "-",
    )
    result = _run_bounded_bytes(argv, DECODE_TIMEOUT_SEC, "video luma decode")
    if result.returncode != 0 or result.stderr.strip():
        detail = result.stderr.decode("utf-8", "replace").strip()[-500:]
        raise VisualDecodeError(detail or f"ffmpeg decode exited {result.returncode}")
    payload = result.stdout
    frame_bytes = DECODE_W * DECODE_H
    if len(payload) % frame_bytes != 0:
        raise VisualDecodeError(
            f"raw luma length {len(payload)} is not a multiple of {frame_bytes}"
        )
    count = len(payload) // frame_bytes
    if count != facts.frame_count:
        raise VisualDecodeError(
            f"decoded {count} frames but the stream probes as {facts.frame_count}"
        )
    return tuple(
        LumaFrame(
            frame_index=index,
            pts=frame_pts(facts, index),
            luma=payload[index * frame_bytes : (index + 1) * frame_bytes],
        )
        for index in range(count)
    )


def bind_decode(
    media: Path,
    media_sha256: str,
    facts: VisualStreamFacts,
    *,
    tools: PinnedAudioTools,
) -> tuple[DecodeBinding, tuple[LumaFrame, ...]]:
    binding = DecodeBinding(
        media_path=str(media),
        media_sha256=media_sha256,
        facts=facts,
        decode_w=DECODE_W,
        decode_h=DECODE_H,
        decode_filter=DECODE_FILTER,
        ffmpeg_sha256=tools.ffmpeg_sha256,
        ffprobe_sha256=tools.ffprobe_sha256,
    )
    return binding, decode_luma_frames(media, facts, tools=tools)


__all__ = [
    "LumaFrame",
    "bind_decode",
    "decode_luma_frames",
    "ensure_decode_budget",
    "frame_pts",
    "probe_video_facts",
    "resolve_audio_tools",
]
