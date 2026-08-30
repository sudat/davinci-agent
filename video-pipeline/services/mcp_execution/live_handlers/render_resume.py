"""Trusted-metadata rerun/resume for the native render (Task 7).

An existing output is reused ONLY with a trusted prior record: strict
``NativeRenderMeta`` parse, exact deterministic path, real prior job id
(the sentinel is unrepresentable), sha256 bound to the current bytes, and
EVERY recorded measured fact equal to an independent ffprobe
re-measurement — including ``has_subtitle_stream`` and exact duration; no
tolerance may let the report repeat tampered values. Anything else is
stale/untrusted and is discarded (only after that decision) so the fresh
render stays deterministic.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.mcp_execution.live_handlers.common import (  # noqa: TC001
    LiveSessionContext,
)
from services.mcp_execution.native_render_media import (
    ProbeStreams,
    load_ffprobe,
    validate_native_media,
)
from services.mcp_execution.native_render_meta import NativeRenderMeta
from services.mcp_execution.plan_payloads import (  # noqa: TC001
    RenderNativeParams,
)


def _expected_output(target: Path, custom_name: str) -> Path:
    return target / f"{custom_name}.mp4"


def _validate_expected_media(
    path: Path, p: RenderNativeParams, ffprobe_bin: Path
) -> ProbeStreams:
    return validate_native_media(
        path,
        ffprobe_bin,
        expected_width=p.width,
        expected_height=p.height,
        expected_fps=p.frame_rate,
        expected_video_codec=p.codec_id,
        expected_audio_codec="aac",
        expected_channels=2,
        expected_sample_rate=48000,
    )


def _facts_match_recorded(meta: NativeRenderMeta, probe: ProbeStreams) -> bool:
    """Exact equality on EVERY measured fact — drift is never tolerated."""

    return (
        meta.video_codec.lower() == probe.video_codec.lower()
        and meta.audio_codec.lower() == probe.audio_codec.lower()
        and meta.width == probe.width
        and meta.height == probe.height
        and meta.avg_frame_rate == probe.avg_frame_rate
        and meta.audio_channels == probe.audio_channels
        and meta.audio_sample_rate == probe.audio_sample_rate
        and meta.duration_seconds == probe.duration_seconds
        and meta.has_subtitle_stream == probe.has_subtitle_stream
    )


def trusted_reuse(  # noqa: PLR0911 (each trust gate is its own refusal)
    target: Path, p: RenderNativeParams
) -> dict[str, object] | None:
    """Reuse the existing output only with full trusted-metadata proof.

    Invalid or stale media/metadata falls through to a fresh render by
    returning None — media-validation problems here are NEVER terminal.
    """

    expected = _expected_output(target, p.custom_name)
    meta_path = target / f"{p.custom_name}.meta.json"
    if not expected.is_file() or expected.stat().st_size == 0:
        return None
    if not meta_path.is_file():
        return None
    try:
        meta = NativeRenderMeta.model_validate_json(meta_path.read_bytes())
    except ValidationError:
        return None
    if meta.custom_name != p.custom_name:
        return None
    if Path(meta.output_path) != expected:
        return None
    if sha256_file(expected) != meta.output_sha256:
        return None
    try:
        probe = _validate_expected_media(expected, p, load_ffprobe())
    except Exception:  # noqa: BLE001 — any invalid/stale media is just untrusted
        return None
    if not _facts_match_recorded(meta, probe):
        return None
    return {
        "custom_name": p.custom_name,
        "job_id": meta.job_id,
        "output_sha256": meta.output_sha256,
        "output_path": str(expected),
        "reused": True,
        "media": probe.model_dump(),
    }


def discard_untrusted(target: Path, custom_name: str) -> None:
    """Remove an untrusted output + stale record so the fresh render is
    deterministic (called ONLY after the trust checks above returned None)."""

    expected = _expected_output(target, custom_name)
    if expected.is_file():
        expected.unlink()
    meta_path = target / f"{custom_name}.meta.json"
    if meta_path.is_file():
        meta_path.unlink()


def persist_meta(  # noqa: PLR0913 (one argument per persisted fact)
    ctx: LiveSessionContext,  # noqa: ARG001 (uniform handler seam)
    target: Path,
    p: RenderNativeParams,
    *,
    job_id: str,
    output: Path,
    sha: str,
    probe: ProbeStreams,
    reused: bool,
) -> dict[str, object]:
    """Atomically persist identity + measured facts; echo the result."""

    meta = NativeRenderMeta(
        schema_version="native-render-meta-v1",
        custom_name=p.custom_name,
        job_id=job_id,
        output_path=str(output),
        output_sha256=sha,
        duration_seconds=probe.duration_seconds,
        video_codec=probe.video_codec,
        width=probe.width,
        height=probe.height,
        avg_frame_rate=probe.avg_frame_rate,
        audio_codec=probe.audio_codec,
        audio_channels=probe.audio_channels,
        audio_sample_rate=probe.audio_sample_rate,
        has_subtitle_stream=probe.has_subtitle_stream,
    )
    atomic_write(target / f"{p.custom_name}.meta.json", canonical_model_bytes(meta))
    return {
        "custom_name": p.custom_name,
        "job_id": job_id,
        "output_sha256": sha,
        "output_path": str(output),
        "reused": reused,
        "media": probe.model_dump(),
    }


__all__ = ["discard_untrusted", "persist_meta", "trusted_reuse"]
