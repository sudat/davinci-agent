"""Strict ffprobe media validation for the native render lifecycle.

Lives outside the live-handler package so both the render handler and the
finishing CLI reconcile media through this one boundary; there is no
second writer here — probing is read-only.
"""

from __future__ import annotations

import math
from pathlib import Path

from services.mcp_execution.live_errors import LiveAdapterError
from services.mcp_execution.native_render_probe import (
    ProbeStreams,
    _ffprobe_payload,
    load_ffprobe,
    probe_video_frame_count,
)


def _require_streams(
    payload: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], list[object]]:
    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        raise LiveAdapterError("render-probe-malformed", "no streams")
    video = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None
    )
    audio = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"), None
    )
    if not isinstance(video, dict):
        raise LiveAdapterError("render-no-video-stream", "no video stream")
    if not isinstance(audio, dict):
        raise LiveAdapterError("render-no-audio-stream", "no audio stream")
    return video, audio, streams


def _measured_duration(fmt: dict[str, object]) -> float:
    raw = fmt.get("duration")
    if raw is None:
        raise LiveAdapterError("render-duration-missing", "format duration missing")
    try:
        duration = float(str(raw))
    except (ValueError, TypeError) as exc:
        raise LiveAdapterError("render-duration-invalid", str(exc)) from exc
    if not math.isfinite(duration) or duration <= 0:
        raise LiveAdapterError(
            "render-duration-invalid", f"duration {duration!r} not finite positive"
        )
    return duration


_FPS_TOLERANCE = 0.01


def parse_frame_rate(raw: str) -> float:
    """Parse ffprobe's ``num/den`` (or plain float) frame rate strictly."""

    try:
        if "/" in raw:
            num, den = raw.split("/", 1)
            value = float(num) / float(den) if float(den) != 0 else 0.0
        else:
            value = float(raw)
    except (ValueError, ZeroDivisionError) as exc:
        raise LiveAdapterError("render-fps-invalid", str(exc)) from exc
    if not math.isfinite(value) or value <= 0:
        raise LiveAdapterError("render-fps-invalid", f"non-positive frame rate {raw!r}")
    return value


def _measured_fps(video: dict[str, object], expected_fps: float) -> str:
    fps_raw = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")
    if not fps_raw or fps_raw == "0/1":
        raise LiveAdapterError("render-fps-missing", "avg_frame_rate missing")
    fps = parse_frame_rate(fps_raw)
    if abs(fps - expected_fps) > _FPS_TOLERANCE:
        raise LiveAdapterError("render-fps-mismatch", f"fps {fps} != {expected_fps}")
    return fps_raw


def validate_native_media(  # noqa: C901, PLR0913, PLR0917 (one expected field per arg)
    path: Path,
    ffprobe_bin: Path,
    expected_width: int,
    expected_height: int,
    expected_fps: float,
    expected_video_codec: str,
    expected_audio_codec: str,
    expected_channels: int,
    expected_sample_rate: int,
) -> ProbeStreams:
    if not path.is_file():
        raise LiveAdapterError("render-output-missing", f"no file at {path}")
    if path.stat().st_size == 0:
        raise LiveAdapterError("render-output-zero-bytes", f"{path} is empty")
    payload = _ffprobe_payload(ffprobe_bin, path)
    video, audio, streams = _require_streams(payload)
    fmt = payload.get("format")
    if not isinstance(fmt, dict):
        raise LiveAdapterError("render-probe-malformed", "no format")
    duration = _measured_duration(fmt)
    vcodec = str(video.get("codec_name") or "")
    if vcodec.lower() != expected_video_codec.lower():
        raise LiveAdapterError(
            "render-codec-mismatch", f"video codec {vcodec!r} != {expected_video_codec!r}"
        )
    def _as_int(value: object, label: str) -> int:
        if not isinstance(value, int | str):
            raise LiveAdapterError("render-geometry-missing", f"{label} not numeric: {value!r}")
        return int(value)

    try:
        width = _as_int(video.get("width"), "width")
        height = _as_int(video.get("height"), "height")
    except (KeyError, ValueError, TypeError) as exc:
        raise LiveAdapterError("render-geometry-missing", str(exc)) from exc
    if width != expected_width or height != expected_height:
        raise LiveAdapterError(
            "render-geometry-mismatch",
            f"geometry {width}x{height} != {expected_width}x{expected_height}",
        )
    fps_raw = _measured_fps(video, expected_fps)
    acodec = str(audio.get("codec_name") or "")
    if acodec.lower() != expected_audio_codec.lower():
        raise LiveAdapterError(
            "render-audio-codec-mismatch",
            f"audio codec {acodec!r} != {expected_audio_codec!r}",
        )
    def _audio_int(value: object, label: str) -> int:
        if not isinstance(value, int | str):
            raise LiveAdapterError("render-audio-missing", f"{label} not numeric: {value!r}")
        return int(value)

    try:
        channels = _audio_int(audio.get("channels"), "channels")
        sample_rate = _audio_int(audio.get("sample_rate"), "sample_rate")
    except (KeyError, ValueError, TypeError) as exc:
        raise LiveAdapterError("render-audio-missing", str(exc)) from exc
    if channels != expected_channels:
        raise LiveAdapterError(
            "render-audio-channels-mismatch", f"channels {channels} != {expected_channels}"
        )
    if sample_rate != expected_sample_rate:
        raise LiveAdapterError(
            "render-audio-sample-rate-mismatch",
            f"sample_rate {sample_rate} != {expected_sample_rate}",
        )
    has_sub = any(isinstance(s, dict) and s.get("codec_type") == "subtitle" for s in streams)
    return ProbeStreams(
        video_codec=vcodec,
        width=width,
        height=height,
        avg_frame_rate=fps_raw,
        duration_seconds=duration,
        audio_codec=acodec,
        audio_channels=channels,
        audio_sample_rate=sample_rate,
        has_subtitle_stream=has_sub,
    )


__all__ = [
    "ProbeStreams",
    "_ffprobe_payload",
    "load_ffprobe",
    "parse_frame_rate",
    "probe_video_frame_count",
    "validate_native_media",
]
