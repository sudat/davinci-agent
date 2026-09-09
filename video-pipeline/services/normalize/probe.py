"""Pinned-ffprobe media facts for normalize input/output verification.

Frame counts are metadata-first: when the container/stream metadata provides
a trustworthy basis, the count is taken WITHOUT a full decode (stream
``nb_frames`` when present and consistent with duration x fps, else
duration x fps rounded) and RECORDED as derived via
``VideoFacts.frame_count_source`` — a derived count is never presented as a
measured decode. The bounded full-decode ``-count_frames`` run survives as
the fallback for absent/inconsistent metadata, with its scaled timeout and
typed ``ProbeExecutionError`` untouched, so genuinely undecodable input
still fails honestly. All temporal fields stay integer ticks plus
rationals; ffprobe floats never enter canonical storage.
"""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from math import ceil
from pathlib import Path
from typing import Final, Literal, TypeGuard

from services.contracts.primitives import StrictModel
from services.ingest.probe import (
    ProbeExecutionError,
    decimal_to_fraction,
    parse_rate_rational,
)
from services.ingest.records import rotation_degrees as stream_rotation
from services.normalize.errors import NormalizeVerificationError

# Decode-time budgets, SCALED by media size (PRD §2.5 measured blocker: the
# original frozen 120 s constant was sized for <=30 s synthetic fixtures and
# the representative 282 s 4K episode needs an honest budget).
#
# MEASURED 2026-08-24, this machine class (pinned ffmpeg/ffprobe 7.1.1,
# software decode of the REAL v44-real-01 episode and a mezzanine-class
# clip of it):
# - HEVC Main10 4K input, ``ffprobe -count_frames``: 333 s in round 1,
#   437 s re-measured mid-run under sustained load (8459 frames; 39.4 →
#   51.7 ms/frame — run-to-run variance ~+31%, thermal throttling class).
# - H.264 8-bit 4K cfr30 (h264_videotoolbox mezzanine class, 10 s real
#   clip through the locked recipe): ``-count_frames`` 5.67 s / 300 frames
#   = 18.9 ms/frame; decoded rawvideo->sha256 6.82 s / 300 frames
#   = 22.7 ms/frame.
# Budget: 70 ms/frame (+35% over the WORST observed 51.7 ms/frame), a
# 120 s FLOOR so small/test media keep today's behavior and speed, and a
# 1200 s (20 min) hard CEILING so the hung-command guard survives. A real
# decode that outgrows the budget still fails as the SAME typed
# ``ProbeExecutionError`` — the guard is scaled, never removed.
PROBE_TIMEOUT_FLOOR_SECONDS: Final = 120
PROBE_TIMEOUT_CEILING_SECONDS: Final = 1200
DECODE_PER_FRAME_BUDGET_MS: Final = 70
PROBE_ARGUMENTS: Final = (
    "-v",
    "error",
    "-print_format",
    "json",
    "-show_streams",
    "-show_format",
    "-count_frames",
)

FrameCountSource = Literal["stream_metadata", "duration_fps_derived", "decoded"]
"""Accuracy class of ``VideoFacts.nb_read_frames``.

- ``"stream_metadata"``: container ``nb_frames``, cross-checked against
  duration x fps within tolerance.
- ``"duration_fps_derived"``: ``round(duration x fps)`` (half-up, exact
  rational arithmetic) — ``nb_frames`` was absent.
- ``"decoded"``: the pre-existing bounded full-decode ``-count_frames``
  observation (metadata absent/inconsistent).
"""

DERIVED_COUNT_TOLERANCE_FLOOR_FRAMES: Final = 2
DERIVED_COUNT_TOLERANCE_RATIO: Final = Fraction(1, 1000)
HEAD_SAMPLE_PACKETS: Final = 64


def decode_probe_timeout_seconds(frame_hint: int | None) -> int:
    """Media-size-scaled decode budget for a frame-count hint.

    The hint only SIZES the budget; correctness never depends on it — a
    hint that underestimates real footage produces the same typed
    timeout, never a silent pass.
    """

    if frame_hint is None or frame_hint <= 0:
        return PROBE_TIMEOUT_FLOOR_SECONDS
    scaled = ceil(frame_hint * DECODE_PER_FRAME_BUDGET_MS / 1000)
    return min(PROBE_TIMEOUT_CEILING_SECONDS, max(PROBE_TIMEOUT_FLOOR_SECONDS, scaled))


def _metadata_payload(ffprobe: Path, media: Path) -> dict[str, object] | None:
    """Fast metadata-only probe (container claims, no decode).

    Returns the parsed payload, or ``None`` when ffprobe *reports* failure
    (the caller then takes the bounded full-decode path, where truly broken
    media fails typed). A metadata probe that exceeds its own bound still
    raises — the hung-command guard is never downgraded to a silent ``None``.
    """

    argv = (
        str(ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(media),
    )
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_FLOOR_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeExecutionError(
            f"ffprobe metadata probe exceeded the bounded {PROBE_TIMEOUT_FLOOR_SECONDS}s timeout"
        ) from error
    if result.returncode != 0:
        return None
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _first_video_stream(payload: dict[str, object]) -> dict[str, object] | None:
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return None
    for stream in streams:
        if _is_video_stream(stream):
            return stream
    return None


def _stream_duration_seconds(
    video: dict[str, object], payload: dict[str, object]
) -> Fraction | None:
    """Tick-exact stream duration as a rational; ``None`` when not derivable.

    Prefers ``duration_ts`` x ``time_base`` (integer ticks), then the stream
    ``duration`` decimal, then the format ``duration`` decimal — all parsed
    exactly, never via float.
    """

    time_base = video.get("time_base")
    duration_ts = video.get("duration_ts")
    if (
        isinstance(time_base, str)
        and isinstance(duration_ts, (int, str))
        and not isinstance(duration_ts, bool)
    ):
        try:
            num_text, den_text = time_base.split("/", maxsplit=1)
            duration = Fraction(int(duration_ts) * int(num_text), int(den_text))
        except ValueError:
            duration = None
        if duration is not None and duration > 0:
            return duration
    for holder in (video, payload.get("format")):
        if not isinstance(holder, dict):
            continue
        duration_text = holder.get("duration")
        if not isinstance(duration_text, str):
            continue
        try:
            duration = decimal_to_fraction(duration_text)
        except (ValueError, ArithmeticError):
            continue
        if duration > 0:
            return duration
    return None


def _stream_frame_rate(video: dict[str, object]) -> Fraction | None:
    rate_text = video.get("r_frame_rate")
    if not isinstance(rate_text, str):
        return None
    try:
        rate_num, rate_den = parse_rate_rational(rate_text, "r_frame_rate")
    except ProbeExecutionError:
        return None
    return Fraction(rate_num, rate_den)


def _round_half_up(value: Fraction) -> int:
    """Round-half-up for non-negative rationals (frame counts, never banker's)."""

    quotient, remainder = divmod(value.numerator, value.denominator)
    return quotient + (1 if 2 * remainder >= value.denominator else 0)


def _derive_frame_count(
    video: dict[str, object],
    payload: dict[str, object],
) -> tuple[int, FrameCountSource] | None:
    """Frame count from container metadata; ``None`` means fall back to decode.

    ``nb_frames`` is accepted only when it agrees with duration x fps within
    ``max(2 frames, estimate/1000)`` — a wildly inconsistent container claim
    (or VFR-style metadata) takes the honest full-decode path instead.
    Without ``nb_frames``, duration x fps rounded half-up is returned and
    marked as derived. Tick-exact ``duration_ts`` is required in both cases
    — a float-only duration is not enough to skip the decode, because
    ``_video_facts`` needs integer ticks downstream.
    """

    rate = _stream_frame_rate(video)
    duration = _stream_duration_seconds(video, payload)
    ticks_known = _int_field(video, "duration_ts") is not None
    if rate is None or duration is None or not ticks_known:
        return None
    estimate = duration * rate
    if estimate <= 0:
        return None
    rounded = _round_half_up(estimate)
    claimed = _int_field(video, "nb_frames")
    if claimed is None:
        return rounded, "duration_fps_derived"
    tolerance = max(
        DERIVED_COUNT_TOLERANCE_FLOOR_FRAMES,
        int(estimate * DERIVED_COUNT_TOLERANCE_RATIO),
    )
    if claimed > 0 and abs(claimed - rounded) <= tolerance:
        return claimed, "stream_metadata"
    return None


def _head_decode_sample(ffprobe: Path, media: Path) -> None:
    """Bounded partial-decode sanity gate for metadata-derived counts.

    Decodes only the head packet interval — a fixed small budget independent
    of media length, so healthy 4K feature-length clips pass in seconds.
    Judgement is returncode-only: decoder *warnings* on stderr must not block
    healthy footage, only an actual decode failure refuses. Any refusal is a
    ``ProbeExecutionError`` so callers keep the ``undecodable_output`` label.
    This is a sanity gate, not a full proof — the full proof for inputs is
    the transcode itself, for outputs the decoded-frame hash.
    """

    try:
        result = subprocess.run(
            (
                str(ffprobe),
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-count_frames",
                "-read_intervals",
                f"0%+#{HEAD_SAMPLE_PACKETS}",
                str(media),
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_FLOOR_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeExecutionError(
            "ffprobe head-decode sample exceeded the bounded "
            f"timeout of {PROBE_TIMEOUT_FLOOR_SECONDS}s"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or "ffprobe head-decode sample failed"
        raise ProbeExecutionError(f"media head is not decodable: {detail}")


def _payload_with_frame_count(
    payload: dict[str, object], video: dict[str, object], count: int
) -> dict[str, object]:
    """Metadata payload copy with the derived count injected as ``nb_read_frames``.

    The injection keeps ``parse_media_facts`` untouched; honesty lives in
    ``VideoFacts.frame_count_source``, stamped by ``probe_media_facts``.
    """

    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise ProbeExecutionError("ffprobe stream list is malformed")
    video_copy = dict(video)
    video_copy["nb_read_frames"] = count
    return {
        **payload,
        "streams": [video_copy if stream is video else stream for stream in streams],
    }


def _frame_hint(payload: dict[str, object] | None) -> int | None:
    """Fast metadata-only frame estimate (container claims, no decode).

    ``r_frame_rate x duration`` is exact for the CFR media this probe
    verifies and close enough everywhere else — the estimate only scales
    the decode budget of the ``-count_frames`` run that follows. A
    metadata probe that cannot produce a hint returns ``None`` (floor
    budget); a media that is truly broken then fails typed in the real
    probe below.
    """

    if not isinstance(payload, dict):
        return None
    video = _first_video_stream(payload)
    if video is None:
        return None
    rate = _stream_frame_rate(video)
    duration = _stream_duration_seconds(video, payload)
    if rate is None or duration is None:
        return None
    estimate = duration * rate
    return int(estimate) if estimate > 0 else None


def _is_video_stream(stream: object) -> TypeGuard[dict[str, object]]:
    return isinstance(stream, dict) and stream.get("codec_type") == "video"


class VideoFacts(StrictModel):
    codec_name: str
    width: int
    height: int
    pix_fmt: str
    r_frame_rate_num: int
    r_frame_rate_den: int
    avg_frame_rate_num: int
    avg_frame_rate_den: int
    nb_read_frames: int
    frame_count_source: FrameCountSource
    duration_num: int
    duration_den: int
    rotation_degrees: int | None
    color_space: str | None
    color_transfer: str | None
    color_primaries: str | None
    color_range: str | None
    time_base_num: int
    time_base_den: int


class AudioFacts(StrictModel):
    codec_name: str
    sample_rate: int
    channels: int


class MediaFacts(StrictModel):
    video: VideoFacts
    audio: AudioFacts | None


def probe_media_json(ffprobe: Path, media: Path) -> dict[str, object]:
    payload, _source = _probe_payload_and_source(ffprobe, media)
    return payload


def _probe_payload_and_source(
    ffprobe: Path, media: Path
) -> tuple[dict[str, object], FrameCountSource]:
    """Metadata-first probe: derived count when trustworthy, else full decode.

    The metadata probe runs once and serves triple duty — budget hint,
    derivation basis, and (via the head-decode sample) an early typed
    refusal for header-plausible corrupt media. Only absent/inconsistent
    metadata pays for the bounded full-decode ``-count_frames`` run, whose
    timeout and typed errors are unchanged.
    """

    metadata = _metadata_payload(ffprobe, media)
    timeout = decode_probe_timeout_seconds(_frame_hint(metadata))
    if metadata is not None:
        video = _first_video_stream(metadata)
        derived = (
            _derive_frame_count(video, metadata) if video is not None else None
        )
        if derived is not None and video is not None:
            count, source = derived
            _head_decode_sample(ffprobe, media)
            return _payload_with_frame_count(metadata, video, count), source
    try:
        result = subprocess.run(
            (str(ffprobe), *PROBE_ARGUMENTS, str(media)),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeExecutionError(
            f"ffprobe exceeded the bounded timeout of {timeout}s"
        ) from error
    if result.returncode != 0 or result.stderr.strip():
        detail = result.stderr.strip() or "ffprobe exited without success"
        raise ProbeExecutionError(detail)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProbeExecutionError("ffprobe produced invalid JSON") from error
    if not isinstance(payload, dict):
        raise ProbeExecutionError("ffprobe payload is not an object")
    return payload, "decoded"


def _text(stream: dict[str, object], key: str) -> str:
    value = stream.get(key)
    if not isinstance(value, str):
        raise NormalizeVerificationError(
            "silent_metadata_loss", f"video stream is missing field {key!r}"
        )
    return value


def _optional(stream: dict[str, object], key: str) -> str | None:
    value = stream.get(key)
    return value if isinstance(value, str) else None


def _int_field(stream: dict[str, object], key: str) -> int | None:
    value = stream.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _require_int(stream: dict[str, object], key: str) -> int:
    value = _int_field(stream, key)
    if value is None:
        raise NormalizeVerificationError(
            "silent_metadata_loss", f"video stream is missing integer field {key!r}"
        )
    return value


def _video_facts(stream: dict[str, object], *, frame_count_source: FrameCountSource) -> VideoFacts:
    r_num, r_den = parse_rate_rational(_text(stream, "r_frame_rate"), "r_frame_rate")
    a_num, a_den = parse_rate_rational(_text(stream, "avg_frame_rate"), "avg_frame_rate")
    tb_text = _text(stream, "time_base")
    tb_num_text, tb_den_text = tb_text.split("/", maxsplit=1)
    tb_num, tb_den = int(tb_num_text), int(tb_den_text)
    duration_ts = _require_int(stream, "duration_ts")
    nb_read = _require_int(stream, "nb_read_frames")
    width, height = _require_int(stream, "width"), _require_int(stream, "height")
    return VideoFacts(
        codec_name=_text(stream, "codec_name"),
        width=width,
        height=height,
        pix_fmt=_text(stream, "pix_fmt"),
        r_frame_rate_num=r_num,
        r_frame_rate_den=r_den,
        avg_frame_rate_num=a_num,
        avg_frame_rate_den=a_den,
        nb_read_frames=nb_read,
        frame_count_source=frame_count_source,
        duration_num=duration_ts * tb_num,
        duration_den=tb_den,
        rotation_degrees=stream_rotation(stream),
        color_space=_optional(stream, "color_space"),
        color_transfer=_optional(stream, "color_transfer"),
        color_primaries=_optional(stream, "color_primaries"),
        color_range=_optional(stream, "color_range"),
        time_base_num=int(tb_num),
        time_base_den=int(tb_den),
    )


def _audio_facts(stream: dict[str, object]) -> AudioFacts:
    return AudioFacts(
        codec_name=_text(stream, "codec_name"),
        sample_rate=_require_int(stream, "sample_rate") or 0,
        channels=_require_int(stream, "channels") or 0,
    )


def parse_media_facts(
    payload: dict[str, object], *, frame_count_source: FrameCountSource = "decoded"
) -> MediaFacts:
    streams = payload.get("streams")
    if not isinstance(streams, list) or not all(
        isinstance(item, dict) for item in streams
    ):
        raise NormalizeVerificationError(
            "silent_metadata_loss", "ffprobe payload has no stream list"
        )
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise NormalizeVerificationError("silent_metadata_loss", "media has no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return MediaFacts(
        video=_video_facts(video, frame_count_source=frame_count_source),
        audio=_audio_facts(audio) if audio is not None else None,
    )


def probe_media_facts(ffprobe: Path, media: Path) -> MediaFacts:
    payload, source = _probe_payload_and_source(ffprobe, media)
    return parse_media_facts(payload, frame_count_source=source)


def decoded_video_sha256(ffmpeg: Path, media: Path, *, frame_hint: int) -> str:
    """Semantic replay anchor: sha256 over fully decoded raw video frames.

    ``frame_hint`` (the verified output frame count at the call site) only
    scales the decode budget — measured rationale in the module header.
    """

    timeout = decode_probe_timeout_seconds(frame_hint)
    try:
        result = subprocess.run(
            (
                str(ffmpeg), "-v", "error", "-i", str(media), "-map", "0:v:0",
                "-c:v", "rawvideo", "-pix_fmt", "yuv420p", "-f", "hash",
                "-hash", "sha256", "-",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeExecutionError(
            f"decoded-video hash exceeded the bounded timeout of {timeout}s"
        ) from error
    if result.returncode != 0 or not result.stdout.startswith("SHA256="):
        raise ProbeExecutionError("decoded video hash output is malformed")
    return result.stdout.strip().removeprefix("SHA256=")


def stream_duration_seconds(facts: VideoFacts) -> Fraction:
    return Fraction(facts.duration_num, facts.duration_den)
