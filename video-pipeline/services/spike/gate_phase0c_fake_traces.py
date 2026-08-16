"""Synthetic preview traces for offline Phase-0C fake evidence trees.

Builds the same trace-manifest contract the real renderer produces, but from
pure computation over the Timeline IR: a deterministic fake preview payload,
a fabricated-but-consistent ffprobe summary, and the real Todo-27 trace
assembly (coverage, decision chain) so the evaluator's recomputation accepts
the fake tree exactly like a rendered one.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.preview.models import (
    AppliedDecision,
    FfprobeSummary,
    ItemBinding,
    MediaBinding,
    PreviewMediaBindings,
    PreviewTraceManifest,
)
from services.preview.render import extract_layout
from services.preview.trace import TraceContext, build_trace

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIr0C

TRACE_NAME: Final = "preview-trace.json"
PREVIEW_NAME: Final = "preview.mp4"


def synthetic_bindings(ir: TimelineIr0C) -> PreviewMediaBindings:
    items = tuple(
        ItemBinding(
            item_id=item.item_id,
            binding=MediaBinding(
                media_path=f"/synthetic-0c/{item.item_id}.bin", sha256="0" * 64
            ),
        )
        for track in ir.tracks
        for item in track.items
    )
    return PreviewMediaBindings(items=items, bgm=None)


def synthetic_summary(ir: TimelineIr0C) -> FfprobeSummary:
    layout = extract_layout(ir)
    frames = layout.total_record_frames
    return FfprobeSummary(
        stream_count=3 if layout.subtitle_items else 2,
        video_codec="h264",
        width=640,
        height=360,
        r_frame_rate="30/1",
        avg_frame_rate="30/1",
        nb_read_frames=frames,
        video_duration_ms=frames * 1000 // 30,
        container_duration_ms=frames * 1000 // 30,
        audio_codec="aac",
        audio_sample_rate=48000,
        audio_channels=1,
        subtitle_codec="mov_text" if layout.subtitle_items else None,
    )


def synthetic_trace(
    directory: Path,
    ir: TimelineIr0C,
    plan_version: str,
    decision: AppliedDecision | None,
) -> PreviewTraceManifest:
    directory.mkdir(parents=True, exist_ok=True)
    preview = directory / PREVIEW_NAME
    payload = f"synthetic-preview:{directory}".encode()
    atomic_write(preview, payload)
    context = TraceContext(
        ir=ir,
        layout=extract_layout(ir),
        bindings=synthetic_bindings(ir),
        plan_version=plan_version,
        decision=decision,
    )
    trace = build_trace(context, preview, synthetic_summary(ir), sha256_file(preview))
    atomic_write(directory / TRACE_NAME, canonical_model_bytes(trace))
    return trace


__all__ = [
    "PREVIEW_NAME",
    "TRACE_NAME",
    "synthetic_bindings",
    "synthetic_summary",
    "synthetic_trace",
]
