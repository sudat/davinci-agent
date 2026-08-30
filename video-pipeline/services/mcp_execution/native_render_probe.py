"""Pinned ffprobe probe helpers for the native render lifecycle.

Header-based probing only (no decode). Lives beside
``native_render_media``; that module re-exports the probe surface so
existing import paths keep working.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel

from services.mcp_execution.live_errors import LiveAdapterError

_PROBE_TIMEOUT = 60


class ProbeStreams(BaseModel):
    video_codec: str
    width: int
    height: int
    avg_frame_rate: str
    duration_seconds: float
    audio_codec: str
    audio_channels: int
    audio_sample_rate: int
    has_subtitle_stream: bool


def _ffprobe_payload(ffprobe_bin: Path, media: Path) -> dict[str, object]:
    result = subprocess.run(
        (
            str(ffprobe_bin),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(media),
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=_PROBE_TIMEOUT,
    )
    if result.returncode != 0:
        raise LiveAdapterError("render-media-probe-failed", result.stderr[-300:])
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LiveAdapterError("render-probe-malformed", str(exc)) from exc
    if not isinstance(payload, dict):
        raise LiveAdapterError("render-probe-malformed", "ffprobe payload not a dict")
    return payload


def load_ffprobe() -> Path:
    """The pinned QC ffprobe binary (hash-verified toolchain)."""

    from services.qc.tools import load_qc_tools  # noqa: PLC0415

    try:
        return load_qc_tools().ffprobe
    except Exception as exc:
        raise LiveAdapterError("render-probe-unavailable", str(exc)) from exc


def probe_video_frame_count(path: Path, ffprobe_bin: Path) -> int:
    """Exact total video frame count of a media file (ffprobe ``nb_frames``).

    Strict and header-based (no decode): a container that reports no frame
    count fails typed rather than guessing — the EOF-aware placement
    reconciliation stays inactive without a trusted count.
    """

    payload = _ffprobe_payload(ffprobe_bin, path)
    streams = payload.get("streams")
    video = next(
        (
            stream
            for stream in (streams if isinstance(streams, list) else ())
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ),
        None,
    )
    raw = video.get("nb_frames") if video is not None else None
    if raw is None:
        raise LiveAdapterError(
            "media-frame-count-missing", f"{path.name} reports no video nb_frames"
        )
    try:
        count = int(str(raw))
    except ValueError as exc:
        raise LiveAdapterError(
            "media-frame-count-invalid", f"{path.name} nb_frames {raw!r}"
        ) from exc
    if count <= 0:
        raise LiveAdapterError(
            "media-frame-count-invalid", f"{path.name} nb_frames {count!r}"
        )
    return count


__all__ = ["ProbeStreams", "_ffprobe_payload", "load_ffprobe", "probe_video_frame_count"]
