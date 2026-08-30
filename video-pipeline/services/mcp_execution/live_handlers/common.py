"""Shared live-session state and the cross-domain handler utilities.

Internal seam beneath ``LiveMcpAdapter`` (the only public product mutation
adapter). Owns the typed mutable session context shared by all live
handlers plus the param/readback validators every handler consumes. The
session/placement handlers (prepare, import, append) live in
``placement.py`` — the Task-3 responsibility boundary, split once this
module crossed the 250 pure-LOC ceiling. Logical product surface names
never reach the vendor transport from here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from services.mcp_client.execution_runner import McpTransportFn  # noqa: TC001
from services.mcp_execution.live_errors import (
    LiveAdapterError,
    LiveAdapterUnsupportedError,
)

if TYPE_CHECKING:
    from services.mcp_client.ops_models import McpActionOutcome, TrackItemsResult
    from services.mcp_execution.audio_measurement import AudioMeasureFn
    from services.mcp_execution.color_measurement import FrameDiffFn


@dataclass
class LiveSessionContext:
    """Mutable session state shared by every live handler invocation.

    One context per adapter run: handlers read the current project/timeline
    identity and timeline start after prepare, record imported clip ids,
    and resolve source ids to media paths through these fields only. The
    Task 5 audio stages additionally consume the injected rendered-media
    measurement port and its render directory; the Task 6 color stage
    consumes the render directory plus the frame-comparison port (None =
    refuse typed).
    """

    transport: McpTransportFn
    media_paths: dict[str, str]
    source_paths: dict[str, str]
    timeline_start: int | None = None
    clip_ids: dict[str, str] = field(default_factory=dict)
    path_for_source: dict[str, str] = field(default_factory=dict)
    current_project_name: str | None = None
    current_timeline_name: str | None = None
    current_timeline_id: str | None = None
    audio_measure: AudioMeasureFn | None = None
    render_dir: str | None = None
    frame_diff: FrameDiffFn | None = None
    #: Independently established (ffprobe nb_frames) per-source media frame
    #: EOF. Placement reconciliation consults it ONLY for the measured
    #: media-end source readback drift (see placement.py); empty = unknown,
    #: which keeps the ordinary ±1 tolerance rule.
    media_frame_counts: dict[str, int] = field(default_factory=dict)
    #: True once any handler mutated rendered timeline CONTENT in this
    #: session (placement, subtitle card, grade, audio state). The native
    #: render uses it to refuse reusing a prior render that predates the
    #: current timeline state (render currency, Task 8).
    timeline_mutated: bool = False
    #: Session-scoped BOUNDED per-track scan snapshots (one per track type)
    #: reused by placement reconciliation: the whole-timeline
    #: source_range_report walk stopped completing within 900 s on the
    #: fully-built representative timeline, so placement reconciles through
    #: get_items_in_track + targeted per-item source-frame readback instead.
    #: Cleared by every content mutation; never trusted across sessions.
    placement_scan: dict[str, TrackItemsResult] = field(default_factory=dict)

    def mark_timeline_mutated(self) -> None:
        """Record a content mutation: invalidates prior renders AND the
        cached placement scans (the next placement re-scans its track)."""

        self.timeline_mutated = True
        self.placement_scan.clear()

    @classmethod
    def build(  # noqa: PLR0913 (keyword surface mirrors LiveMcpAdapter's injected ports)
        cls,
        transport: McpTransportFn,
        media_paths: Mapping[str, str] | None,
        *,
        audio_measure: AudioMeasureFn | None = None,
        render_dir: str | None = None,
        frame_diff: FrameDiffFn | None = None,
        media_frame_counts: Mapping[str, int] | None = None,
    ) -> LiveSessionContext:
        """Normalize caller keying (id→path or path→id) to id→path."""

        raw = dict(media_paths or {})
        if raw and any(k.startswith("/") for k in raw):
            raw = {v: k for k, v in raw.items()}
        return cls(
            transport=transport,
            media_paths=raw,
            source_paths=dict(raw),
            audio_measure=audio_measure,
            render_dir=render_dir,
            frame_diff=frame_diff,
            media_frame_counts=dict(media_frame_counts or {}),
        )


def validate_params[T: BaseModel](
    model: type[T], params: Mapping[str, object]
) -> T:
    """Typed params boundary: invalid shapes stay loud, never coerced."""

    try:
        return model.model_validate(params)
    except ValidationError as exc:
        raise LiveAdapterError("params-invalid", str(exc)) from exc


def require_ok(outcome: McpActionOutcome, label: str) -> None:
    if not outcome.ok:
        raise LiveAdapterError(f"{label}-failed", f"{label} not ok: {outcome.error}")


__all__ = [
    "LiveAdapterError",
    "LiveAdapterUnsupportedError",
    "LiveSessionContext",
    "require_ok",
    "validate_params",
]
