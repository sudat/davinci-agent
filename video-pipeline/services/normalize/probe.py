"""Pinned-ffprobe media facts for normalize input/output verification.

Uses ``-count_frames`` so ``nb_read_frames`` is a decode-level observation
(decodability), not a container claim. All temporal fields stay integer ticks
plus rationals; ffprobe floats never enter canonical storage.
"""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Final

from services.contracts.primitives import StrictModel
from services.ingest.probe import ProbeExecutionError, parse_rate_rational
from services.ingest.records import rotation_degrees as stream_rotation
from services.normalize.errors import NormalizeVerificationError

PROBE_TIMEOUT_SECONDS: Final = 120
PROBE_ARGUMENTS: Final = (
    "-v",
    "error",
    "-print_format",
    "json",
    "-show_streams",
    "-show_format",
    "-count_frames",
)


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
    try:
        result = subprocess.run(
            (str(ffprobe), *PROBE_ARGUMENTS, str(media)),
            check=False,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeExecutionError("ffprobe exceeded the bounded timeout") from error
    if result.returncode != 0 or result.stderr.strip():
        detail = result.stderr.strip() or "ffprobe exited without success"
        raise ProbeExecutionError(detail)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProbeExecutionError("ffprobe produced invalid JSON") from error
    if not isinstance(payload, dict):
        raise ProbeExecutionError("ffprobe payload is not an object")
    return payload


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


def _video_facts(stream: dict[str, object]) -> VideoFacts:
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


def parse_media_facts(payload: dict[str, object]) -> MediaFacts:
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
        video=_video_facts(video),
        audio=_audio_facts(audio) if audio is not None else None,
    )


def probe_media_facts(ffprobe: Path, media: Path) -> MediaFacts:
    return parse_media_facts(probe_media_json(ffprobe, media))


def decoded_video_sha256(ffmpeg: Path, media: Path) -> str:
    """Semantic replay anchor: sha256 over fully decoded raw video frames."""

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
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeExecutionError("decoded-video hash exceeded the timeout") from error
    if result.returncode != 0 or not result.stdout.startswith("SHA256="):
        raise ProbeExecutionError("decoded video hash output is malformed")
    return result.stdout.strip().removeprefix("SHA256=")


def stream_duration_seconds(facts: VideoFacts) -> Fraction:
    return Fraction(facts.duration_num, facts.duration_den)
