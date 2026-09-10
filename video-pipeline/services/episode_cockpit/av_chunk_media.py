"""Episode media for the chunked AV observation: proxy + per-core clips.

The low-res A/V proxy built once per Edit Source and one context-
extended chunk clip per core; both share the VideoToolbox recipe (the
pinned toolchain ffmpeg is a VideoToolbox-only build, no libx264) and
skip re-encode when the fingerprint sidecar matches. Proxies and clips
are rebuildable runtime state under the episode dir.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import atomic_write
from services.media_intelligence.sample_observation import SampleObservationError

if TYPE_CHECKING:
    from services.media_intelligence.gemini_av_models import AvChunk

_MEZZANINE_RELATIVE = ("run", "media", "edit-source.mov")
_AV_PROXY_DIR_NAME = "av-proxy"
_AV_PROXY_NAME = "proxy-480p-audio.mp4"
_AV_PROXY_SOURCE_SIDECAR = "proxy-480p-audio.source.json"
#: Whole-proxy encode budget (the 282s A/B proxy builds in well under this).
_AV_PROXY_TIMEOUT_S = 600
_AV_CHUNK_DIR_NAME = "av-chunks"
_AV_CHUNK_TIMEOUT_S = 300


def ensure_av_proxy(*, ffmpeg: Path, mezzanine: Path, workspace_dir: Path) -> Path:
    """Build the low-res A/V proxy once per Edit Source.

    ``ffmpeg -vf scale=480:-2 -c:v h264_videotoolbox -b:v 250k -maxrate 350k
    -bufsize 700k -c:a aac -b:a 96k -movflags +faststart`` — sized to the
    A/B-measured proxy (282s→7.7MB, 25,662 VIDEO tokens ≈ $0.008/call at LOW
    resolution). The pinned toolchain ffmpeg is a VideoToolbox-only build
    (no libx264), so crf is not an option there. Rebuilds only when the
    Edit Source size/mtime moved; the proxy is rebuildable runtime state
    under the episode dir.
    """

    from services.analyze.audio_probe import run_bounded  # noqa: PLC0415 (bounded run)

    workspace_dir.mkdir(parents=True, exist_ok=True)
    target = workspace_dir / _AV_PROXY_NAME
    sidecar = workspace_dir / _AV_PROXY_SOURCE_SIDECAR
    try:
        stat = mezzanine.stat()
        fingerprint = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError as error:
        raise SampleObservationError(
            "sample-av-unavailable",
            f"cannot stat the edit source media: {type(error).__name__}",
        ) from error
    if target.is_file() and sidecar.is_file():
        try:
            if json.loads(sidecar.read_text(encoding="utf-8")) == fingerprint:
                return target
        except (OSError, ValueError):
            pass
    argv = (
        str(ffmpeg),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        str(mezzanine),
        "-vf",
        "scale=480:-2",
        "-c:v",
        "h264_videotoolbox",
        "-b:v",
        "250k",
        "-maxrate",
        "350k",
        "-bufsize",
        "700k",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(target),
    )
    try:
        result = run_bounded(argv, _AV_PROXY_TIMEOUT_S, "AV proxy encode")
    except Exception as error:
        raise SampleObservationError(
            "sample-av-proxy-failed",
            f"the AV proxy encode failed ({type(error).__name__}); no detail echoed",
        ) from error
    if result.returncode != 0 or not target.is_file():
        raise SampleObservationError(
            "sample-av-proxy-failed",
            "the AV proxy encode failed; ffmpeg stderr is suppressed "
            "(paths never enter typed errors)",
        )
    atomic_write(sidecar, (json.dumps(fingerprint, sort_keys=True) + "\n").encode())
    return target


def _extract_av_chunk(
    *,
    ffmpeg: Path,
    proxy: Path,
    chunk: AvChunk,
    workspace_dir: Path,
) -> Path:
    """Slice one context-extended chunk clip from the whole proxy.

    Same VideoToolbox recipe as the proxy (the pinned toolchain build has
    no libx264). Skips re-encode when the proxy fingerprint + clip bounds
    match the sidecar; the clip is rebuildable runtime state.
    """

    from services.analyze.audio_probe import run_bounded  # noqa: PLC0415 (bounded run)

    workspace_dir.mkdir(parents=True, exist_ok=True)
    target = workspace_dir / f"chunk-{chunk.index:03d}.mp4"
    sidecar = workspace_dir / f"chunk-{chunk.index:03d}.source.json"
    try:
        stat = proxy.stat()
        fingerprint = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "clip_start": chunk.clip_start,
            "clip_end": chunk.clip_end,
        }
    except OSError as error:
        raise SampleObservationError(
            "sample-av-unavailable",
            f"cannot stat the AV proxy: {type(error).__name__}",
        ) from error
    if target.is_file() and sidecar.is_file():
        try:
            if json.loads(sidecar.read_text(encoding="utf-8")) == fingerprint:
                return target
        except (OSError, ValueError):
            pass
    duration = chunk.clip_end - chunk.clip_start
    argv = (
        str(ffmpeg),
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{chunk.clip_start:.3f}",
        "-i",
        str(proxy),
        "-t",
        f"{duration:.3f}",
        "-vf",
        "scale=480:-2",
        "-c:v",
        "h264_videotoolbox",
        "-b:v",
        "250k",
        "-maxrate",
        "350k",
        "-bufsize",
        "700k",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(target),
    )
    try:
        result = run_bounded(argv, _AV_CHUNK_TIMEOUT_S, "AV chunk encode")
    except Exception as error:
        raise SampleObservationError(
            "sample-av-proxy-failed",
            f"the AV chunk encode failed ({type(error).__name__}); no detail echoed",
        ) from error
    if result.returncode != 0 or not target.is_file():
        raise SampleObservationError(
            "sample-av-proxy-failed",
            "the AV chunk encode failed; ffmpeg stderr is suppressed "
            "(paths never enter typed errors)",
        )
    atomic_write(sidecar, (json.dumps(fingerprint, sort_keys=True) + "\n").encode())
    return target


__all__ = ["ensure_av_proxy"]
