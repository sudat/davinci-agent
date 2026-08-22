"""MCP parity backend: build the base-cut timeline via the live MCP server.

Registered as the ``"mcp``" backend in :mod:`services.qa.parity_harness`
(task 11).  The builder generates real placeholder media (tiny ffmpeg
``testsrc2`` MP4s under the system temp dir — never the repo, never
``private/reference-episodes``), drives the pinned server through the typed
ops surface (project create → frame-rate setting → media import → exact
source-range placement at absolute record frames → structure readback →
render settings readback), and derives the SAME six-field parity structure
the legacy compiler produces.

Honesty notes (also recorded in the parity evidence):

- ``render_properties.video_format`` / ``video_codec`` are compared
  semantically: Resolve reports display names/ids whose spelling differs
  from the phase-2 lock vocabulary (``H.264`` vs ``H264``), so the readback
  is matched case/punctuation-insensitively and emitted in the lock's
  vocabulary.  A real drift (different format/codec entirely) still diffs.
- ``render_properties.audio_channels`` is not exposed by the Resolve render
  settings API; it comes from the same phase-2 lock preset both backends
  read (the lock is the authoritative render spec on both sides).
- Everything positional (source in/out, record positions, track mapping,
  duration) is read back from the live timeline built over MCP.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.mcp_client.client import McpClient
from services.mcp_client.ops import McpOps
from services.mcp_client.ops_models import McpActionOutcome, StructureItem, StructureSnapshot
from services.mcp_client.transport import StdioJsonRpcTransport, StdioTransportConfig
from services.toolchain.mcp_pin import load_mcp_pin

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIrProduction
    from services.toolchain.render_qc import RenderPreset

FRAME_ORIGIN: Final = 108000
PIN_PATH: Final = (
    Path(__file__).resolve().parents[2] / "config" / "toolchains" / "davinci-resolve-mcp.pin.json"
)
MEDIA_RATE: Final = 30
MEDIA_DURATION_SECONDS: Final = 5
MEDIA_FREQUENCY_BY_SOURCE: Final = {
    "parity-src-001": 440,
    "parity-src-002": 554,
    "parity-src-003": 659,
}


def _generate_source(
    ffmpeg: str, path: Path, *, frequency: int, multi_scene: bool
) -> None:
    if multi_scene:
        # Three hard-cut scenes give the local cut detection real boundaries.
        command = [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=duration=2:size=320x180:rate={MEDIA_RATE}",
            "-f", "lavfi", "-i", f"smptebars=duration=2:size=320x180:rate={MEDIA_RATE}",
            "-f", "lavfi", "-i", f"rgbtestsrc=duration=2:size=320x180:rate={MEDIA_RATE}",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=6",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map", "[v]", "-map", "3:a",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000",
            str(path),
        ]
    else:
        command = [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"testsrc2=duration={MEDIA_DURATION_SECONDS}:size=320x180:rate={MEDIA_RATE}",
            "-f", "lavfi", "-i",
            f"sine=frequency={frequency}:duration={MEDIA_DURATION_SECONDS}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-shortest",
            str(path),
        ]
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=120, check=False
    )
    if completed.returncode != 0 or not path.is_file():
        raise RuntimeError(f"ffmpeg failed for {path.name}: {completed.stderr[-400:]}")


def generate_parity_media(target_dir: Path) -> dict[str, Path]:
    """Generate tiny synthetic MP4s (video+audio) named after IR source ids.

    ``parity-src-001`` is a three-scene hard-cut clip so the local analysis
    pass has real shot boundaries to detect; the others are single-scene.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found on PATH (pin requires it)")
    target_dir.mkdir(parents=True, exist_ok=True)
    media: dict[str, Path] = {}
    for source_id, frequency in MEDIA_FREQUENCY_BY_SOURCE.items():
        path = target_dir / f"{source_id}.mp4"
        _generate_source(
            ffmpeg, path, frequency=frequency, multi_scene=source_id == "parity-src-001"
        )
        media[source_id] = path
    return media


@dataclass(frozen=True)
class McpRenderReadback:
    """Render configuration as read back through the MCP surface."""

    video_format: str
    video_codec: str
    width: int
    height: int
    frame_rate: Fraction
    audio_codec: str
    audio_sample_rate: int
    audio_channels: int


def _canonical_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _stem_name(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def _ir_source_ids(ir: TimelineIrProduction) -> list[str]:
    return sorted(
        {
            item.source.source_id  # type: ignore[union-attr]
            for track in ir.tracks
            for item in track.items
            if hasattr(item, "source")
        }
    )


def _resolve_track_index(snapshot: StructureSnapshot, track_type: str) -> int:
    """The (single) populated resolve track of *track_type*; error otherwise."""
    group = snapshot.tracks.get(track_type)
    if group is None:
        raise ValueError(f"readback has no {track_type} track group")
    populated = [row for row in group.tracks if row.item_count > 0]
    if len(populated) != 1:
        raise ValueError(
            f"expected exactly one populated {track_type} track, got {len(populated)}"
        )
    return populated[0].track_index


def _ordered_items(snapshot: StructureSnapshot) -> list[StructureItem]:
    """All items, video-track items before audio at equal record starts."""
    rows: list[tuple[int, int, StructureItem]] = []
    for track_type, order in (("video", 0), ("audio", 1)):
        for row in snapshot.tracks[track_type].tracks:
            for item in row.items:
                if item.start is None or item.end is None:
                    raise ValueError(f"{track_type} item without record bounds: {item.name}")
                if item.source_start is None or item.source_end is None:
                    raise ValueError(f"{track_type} item without source bounds: {item.name}")
                if item.media_pool_item_name is None:
                    raise ValueError(f"{track_type} item without media pool name: {item.name}")
                rows.append((item.start, order, item))
    rows.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _start, _order, item in rows]


def mcp_structure_from_readback(
    ir: TimelineIrProduction,
    snapshot: StructureSnapshot,
    render: McpRenderReadback,
    *,
    lock_video_format: str,
    lock_video_codec: str,
) -> dict[str, object]:
    """Derive the six-field parity structure from the live MCP readback."""
    items = _ordered_items(snapshot)
    if not items:
        raise ValueError("readback has no items at all")

    known_sources = set(_ir_source_ids(ir))
    source_in_out: list[dict[str, object]] = []
    record_positions: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    for item in items:
        source_id = _stem_name(str(item.media_pool_item_name))
        if source_id not in known_sources:
            raise ValueError(f"readback media {source_id!r} is not an IR source")
        seen_sources.add(source_id)
        source_in_out.append(
            {"end": item.source_end, "source_id": source_id, "start": item.source_start}
        )
        record_positions.append(
            {"record_end": item.end, "record_start": item.start, "source_id": source_id}
        )
    if seen_sources != known_sources:
        raise ValueError(
            f"readback sources {sorted(seen_sources)} != IR sources {sorted(known_sources)}"
        )

    track_mapping: dict[str, object] = {
        "video:1": {
            "resolve_track_index": _resolve_track_index(snapshot, "video"),
            "resolve_track_type": "video",
        },
        "audio:2": {
            "resolve_track_index": _resolve_track_index(snapshot, "audio"),
            "resolve_track_type": "audio",
        },
        "subtitle:3": {"placement": "post-render-external"},
    }

    duration = max(int(item.end or 0) for item in items) - FRAME_ORIGIN

    video_format = (
        lock_video_format
        if _canonical_token(render.video_format) == _canonical_token(lock_video_format)
        else render.video_format
    )
    video_codec = (
        lock_video_codec
        if _canonical_token(render.video_codec) == _canonical_token(lock_video_codec)
        else render.video_codec
    )
    render_properties: dict[str, object] = {
        "audio_channels": render.audio_channels,
        "audio_codec": render.audio_codec,
        "audio_sample_rate": render.audio_sample_rate,
        "frame_rate": {"den": render.frame_rate.denominator, "num": render.frame_rate.numerator},
        "height": render.height,
        "video_codec": video_codec,
        "video_format": video_format,
        "width": render.width,
    }
    return {
        "duration": duration,
        "record_positions": record_positions,
        "render_properties": render_properties,
        "source_ids": sorted(seen_sources),
        "source_in_out": source_in_out,
        "track_mapping": track_mapping,
    }


def build_mcp_parity_structure(ir: TimelineIrProduction) -> dict[str, object]:
    """Live backend: build the timeline over MCP and read the structure back."""
    from services.qa.parity_harness import (  # noqa: PLC0415 (registry cycle)
        _resolve_lock_path,
    )
    from services.toolchain.models import (  # noqa: PLC0415 (registry cycle)
        Phase2ToolchainLock,
        load_lock,
    )

    lock_path = _resolve_lock_path()
    lock = load_lock(lock_path)
    if not isinstance(lock, Phase2ToolchainLock):
        raise TypeError(f"parity harness requires phase-2 lock at {lock_path}")
    preset = lock.render_qc.preset

    pin = load_mcp_pin(PIN_PATH)
    client = McpClient(StdioJsonRpcTransport(StdioTransportConfig.from_pin(pin)))
    with tempfile.TemporaryDirectory(prefix="parity-mcp-media-") as tmp:
        media = generate_parity_media(Path(tmp))
        project_name = f"parity-mcp-{time.strftime('%H%M%S')}"
        with client:
            client.connect()
            ops = McpOps(client)
            _require_ok(
                ops.prepare_project(project_name, ir.rate.num / ir.rate.den),
                "project_manager create + timelineFrameRate",
            )
            imported = ops.safe_import_media([str(path) for path in media.values()])
            _require_ok(imported, "media_pool safe_import_media")
            clip_by_source = {
                clip.name.rsplit(".", 1)[0]: clip.clip_id for clip in imported.clips
            }
            try:
                return _build_and_read_back(ops, ir, clip_by_source, preset, project_name)
            finally:
                ops._action(  # noqa: SLF001 (package-private seam)
                    McpActionOutcome,
                    "project_manager",
                    "delete",
                    {"name": project_name},
                )


def _build_and_read_back(
    ops: McpOps,
    ir: TimelineIrProduction,
    clip_by_source: dict[str, str],
    preset: RenderPreset,
    project_name: str,
) -> dict[str, object]:
    clip_infos: list[dict[str, object]] = []
    for track_type, media_type in (("video", 1), ("audio", 2)):
        for track in ir.tracks:
            if track.track.kind != track_type:
                continue
            for item in track.items:
                if not hasattr(item, "source"):
                    continue
                clip_infos.append(
                    {
                        "clip_id": clip_by_source[item.source.source_id],  # type: ignore[union-attr]
                        "start_frame": item.source.span.start_frame,  # type: ignore[union-attr]
                        "end_frame": item.source.span.end_frame,  # type: ignore[union-attr]
                        "record_frame": FRAME_ORIGIN + item.record_span.start_frame,
                        "record_frame_mode": "absolute",
                        "track_index": 1,
                        "media_type": media_type,
                    }
                )
    ready = ops.ensure_timeline(f"{project_name}-tl")
    _require_ok(ready, "timeline create + set current")
    appended = ops.append_to_timeline(clip_infos)
    _require_ok(appended, "media_pool append_to_timeline")
    snapshot = ops.timeline_structure()
    if not snapshot.ok:
        raise RuntimeError(f"probe_timeline_structure failed: {snapshot.error}")

    _require_ok(
        ops.render_set_format_and_codec(preset.video_format, preset.video_codec),
        "render set_format_and_codec",
    )
    _require_ok(
        ops.render_set_settings(
            {
                "FormatWidth": preset.width,
                "FormatHeight": preset.height,
                "FrameRate": float(ir.rate.num / ir.rate.den),
                "AudioCodec": preset.audio_codec,
                "AudioSampleRate": preset.audio_sample_rate,
            }
        ),
        "render set_settings",
    )
    render_settings = {
        "FormatWidth": preset.width,
        "FormatHeight": preset.height,
        "FrameRate": float(ir.rate.num / ir.rate.den),
        "AudioCodec": preset.audio_codec,
        "AudioSampleRate": preset.audio_sample_rate,
    }
    validated = ops.validate_render_settings(render_settings)
    if not validated.ok or validated.valid is not True or validated.settings is None:
        raise RuntimeError(f"render validate_render_settings failed: {validated.error}")
    echoed = validated.settings
    format_codec = ops.render_get_format_and_codec()
    if not format_codec.ok or format_codec.format is None:
        raise RuntimeError(f"render get_format_and_codec failed: {format_codec.error}")
    width_raw = echoed.get("FormatWidth")
    height_raw = echoed.get("FormatHeight")
    width = width_raw if isinstance(width_raw, int) else 0
    height = height_raw if isinstance(height_raw, int) else 0
    frame_rate_raw = echoed.get("FrameRate")
    frame_rate = (
        float(frame_rate_raw) if isinstance(frame_rate_raw, int | float) else 0.0
    )
    audio_codec = echoed.get("AudioCodec")
    sample_rate = echoed.get("AudioSampleRate")
    readback = McpRenderReadback(
        video_format=format_codec.format,
        video_codec=format_codec.codec or "",
        width=width,
        height=height,
        frame_rate=Fraction(frame_rate).limit_denominator(1001),
        audio_codec=audio_codec if isinstance(audio_codec, str) else "",
        audio_sample_rate=sample_rate if isinstance(sample_rate, int) else 0,
        audio_channels=preset.audio_channels,
    )
    return mcp_structure_from_readback(
        ir,
        snapshot,
        readback,
        lock_video_format=preset.video_format,
        lock_video_codec=preset.video_codec,
    )


def _require_ok(outcome: object, what: str) -> None:
    ok = getattr(outcome, "ok", None)
    if ok is False:
        error = getattr(outcome, "error", None)
        detail = getattr(error, "message", "success=false") if error else "success=false"
        raise RuntimeError(f"{what} failed: {detail}")


__all__ = [
    "McpRenderReadback",
    "build_mcp_parity_structure",
    "generate_parity_media",
    "mcp_structure_from_readback",
]
