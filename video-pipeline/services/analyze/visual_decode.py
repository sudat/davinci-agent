"""Bounded pinned-ffmpeg raw-luma extraction for the visual checks.

The pinned ffmpeg/ffprobe are hash-verified against the frozen Phase-1
toolchain lock before any use (reusing the Todo-34 resolver). Decode is
bounded twice: by a frame-count budget checked BEFORE any decode, and by a
hard subprocess timeout (floor-constant, scaled per decoded frame — see
``decode_budget_seconds``). Frames come back as raw 8-bit gray bytes
(``-vf scale=W:H,format=gray -f rawvideo``) parsed with the Python stdlib
only — no numpy, no cv2 — and every frame carries its index, its exact PTS
as a reduced rational in stream time_base units, and the source binding.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor
from pathlib import Path
from typing import Final

from services.analyze.analysis_models import AnalyzeRequestError
from services.analyze.audio_probe import PinnedAudioTools, resolve_audio_tools, run_bounded
from services.analyze.visual_constants import (
    DECODE_FILTER,
    DECODE_H,
    DECODE_TIMEOUT_SEC,
    DECODE_W,
    DEFAULT_MAX_DECODE_FRAMES,
    PROBE_TIMEOUT_SEC,
    _resolve_max_decode_frames,
)
from services.analyze.visual_models import (
    DecodeBinding,
    Pts,
    VisualDecodeError,
    VisualStreamFacts,
)

# Decode-time budget scaling. MEASURED 2026-08-24, this machine class
# (pinned ffmpeg 7.1.1, 4K h264_videotoolbox mezzanine-class clip of the
# REAL v44-real-01 footage, 64x36 gray pipeline): 1.24 s / 300 frames
# = 4.1 ms/frame. Budget 10 ms/frame (~2.4x margin) above the 120 s
# DECODE_TIMEOUT_SEC floor, hard-capped at 1200 s so the hung-command
# guard survives for arbitrarily long inputs.
DECODE_BUDGET_PER_FRAME_MS: Final = 10
DECODE_BUDGET_CEILING_SECONDS: Final = 1200
# Accurate-seek rollback margin: the mezzanine GOP is a keyframe every 12
# frames (0.4 s @30fps, measured), so an input seek decodes at most one
# GOP before the window; 60 frames covers any rate up to 120 fps.
_SEEK_ROLLBACK_MARGIN_FRAMES: Final = 60
MAX_DECODE_FRAMES: Final = DEFAULT_MAX_DECODE_FRAMES  # test seam (monkeypatch target)


@dataclass(frozen=True, slots=True)
class LumaFrame:
    frame_index: int
    pts: Pts
    luma: bytes


def ensure_decode_budget(frame_count: int) -> None:
    """Refuse oversized decodes before spending any tool time."""

    env_limit = _resolve_max_decode_frames()
    limit = env_limit
    try:
        import services.analyze.visual_decode as _vd  # noqa: PLC0415, PLW0406

        patched = getattr(_vd, "MAX_DECODE_FRAMES", None)
        if isinstance(patched, int) and patched != DEFAULT_MAX_DECODE_FRAMES:
            limit = patched
    except Exception:  # noqa: BLE001
        limit = env_limit
    if frame_count > limit:
        raise AnalyzeRequestError(
            f"decode budget exceeded: {frame_count} frames > frozen max {limit}"
        )


def decode_budget_seconds(frame_count: int) -> int:
    """Per-frame-scaled decode timeout (floor + measured rate, hard cap).

    MEASURED rationale at ``DECODE_BUDGET_PER_FRAME_MS``; the floor keeps
    small/test media at today's 120 s, the ceiling keeps the hung-command
    guard.
    """

    scaled = ceil(frame_count * DECODE_BUDGET_PER_FRAME_MS / 1000)
    return min(DECODE_BUDGET_CEILING_SECONDS, max(DECODE_TIMEOUT_SEC, scaled))


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
    result = _run_bounded_bytes(
        argv, decode_budget_seconds(facts.frame_count), "video luma decode"
    )
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


def _seek_start_seconds(facts: VisualStreamFacts, start_frame: int) -> str:
    """Exact-ish input-seek timestamp, decimal-truncated DOWN to the micro.

    ffmpeg's accurate input seek drops frames with ``pts < target``:
    truncation toward zero keeps the boundary frame (target sits strictly
    below its pts) while the previous frame is a full frame-duration
    below and stays dropped — measured 2026-08-24: ``-ss 8.999999`` and
    ``-ss 9.0`` both open at frame 270, while ``-ss 9.000001`` shifts to
    frame 271. So: truncate, never round.
    """

    start_seconds = Fraction(start_frame * facts.rate_den, facts.rate_num)
    micros = floor(start_seconds * 1_000_000)
    return f"{micros // 1_000_000}.{micros % 1_000_000:06d}"


def decode_luma_window(
    media: Path,
    facts: VisualStreamFacts,
    *,
    tools: PinnedAudioTools,
    start_frame: int,
    end_frame: int,
) -> tuple[LumaFrame, ...]:
    """Decode ONLY ``[start_frame, end_frame)`` via accurate input seek.

    The whole-file :func:`decode_luma_frames` is the analyzer's tool; deep
    review windows need a bounded slice instead (PRD §2.5 measured
    blocker: decoding the whole 8468-frame mezzanine per review window
    both blew the frame cap and the timeout). The budget cap applies to
    the WINDOW span, and the decoded byte count must equal the span —
    a seek that landed elsewhere or hit EOF is a typed
    :class:`VisualDecodeError`, never silent misalignment.
    """

    if start_frame < 0 or end_frame <= start_frame or end_frame > facts.frame_count:
        raise AnalyzeRequestError(
            f"window [{start_frame}, {end_frame}) is outside the "
            f"{facts.frame_count}-frame stream"
        )
    span = end_frame - start_frame
    ensure_decode_budget(span)
    argv = (
        str(tools.ffmpeg),
        "-nostdin",
        "-v",
        "error",
        "-ss",
        _seek_start_seconds(facts, start_frame),
        "-i",
        str(media),
        "-map",
        "0:v:0",
        "-frames:v",
        str(span),
        "-vf",
        DECODE_FILTER,
        "-f",
        "rawvideo",
        "-",
    )
    result = _run_bounded_bytes(
        argv, decode_budget_seconds(span + _SEEK_ROLLBACK_MARGIN_FRAMES), "window luma decode"
    )
    if result.returncode != 0 or result.stderr.strip():
        detail = result.stderr.decode("utf-8", "replace").strip()[-500:]
        raise VisualDecodeError(detail or f"ffmpeg window decode exited {result.returncode}")
    payload = result.stdout
    frame_bytes = DECODE_W * DECODE_H
    if len(payload) % frame_bytes != 0:
        raise VisualDecodeError(
            f"raw luma length {len(payload)} is not a multiple of {frame_bytes}"
        )
    count = len(payload) // frame_bytes
    if count != span:
        raise VisualDecodeError(
            f"window decode returned {count} frames for span {span} "
            f"[{start_frame}, {end_frame}) — seek misalignment is typed, never guessed"
        )
    return tuple(
        LumaFrame(
            frame_index=start_frame + offset,
            pts=frame_pts(facts, start_frame + offset),
            luma=payload[offset * frame_bytes : (offset + 1) * frame_bytes],
        )
        for offset in range(count)
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
    "DECODE_BUDGET_CEILING_SECONDS",
    "DECODE_BUDGET_PER_FRAME_MS",
    "LumaFrame",
    "bind_decode",
    "decode_budget_seconds",
    "decode_luma_frames",
    "decode_luma_window",
    "ensure_decode_budget",
    "frame_pts",
    "probe_video_facts",
    "resolve_audio_tools",
]
