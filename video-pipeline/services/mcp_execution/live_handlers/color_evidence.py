"""Frame evidence for the Task 6 DRX handler.

Matched-frame proof from fresh before/after renders: the pinned-ffmpeg
comparison port (``color_measurement``) measures the mean luma diff at a
frame index. A graded target frame must clear the diff gate; an untargeted
frame must stay under it — except one independently identified case: a
native subtitle cue card composited over a targeted span changes pixels
because the footage beneath it was graded. That skip requires the card's
identity conjunction, never a span alone (Task 8 repair: the real
episode's targets tile the whole timeline, so a span-only skip made the
gate vacuous).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.mcp_execution.color_measurement import parse_frame_comparison
from services.mcp_execution.live_handlers.color_targets import video_items
from services.mcp_execution.live_handlers.common import LiveAdapterError, LiveSessionContext
from services.mcp_execution.live_handlers.subtitle_card import (
    OVERLAY_TRACK_INDEX,
    is_cue_card_name,
)

if TYPE_CHECKING:
    from services.mcp_client.ops_models import StructureItem, StructureSnapshot

#: Frame-diff gate (Task 4 rendered-span proof precedent): graded frames
#: clear it, codec noise stays below it.
FRAME_DIFF_GATE: Final = 0.5


def frame_changed(diff: object) -> bool:
    return isinstance(diff, float) and diff >= FRAME_DIFF_GATE


def compare_frames(
    ctx: LiveSessionContext, before: Path, after: Path, frame: int
) -> dict[str, object]:
    if ctx.frame_diff is None:
        raise LiveAdapterError(
            "frame-evidence-unavailable", "no rendered-frame comparison port is wired"
        )
    try:
        return parse_frame_comparison(
            ctx.frame_diff(str(before), frame, str(after), frame)
        ).model_dump()
    except ValidationError as error:
        raise LiveAdapterError("frame-evidence-invalid", str(error)) from error


def verify_target_frames(
    ctx: LiveSessionContext,
    entries: list[dict[str, object]],
    before: Path,
    after: Path,
) -> None:
    """Gate every target entry's frame against the diff gate exactly once
    and attach the measured comparison: an already-applied target must be
    STABLE across the rerun (no double-grading drift), a freshly applied
    target must have CHANGED (an apply without a pixel change is not a
    success). Raises typed on either violation."""
    for entry in entries:
        target_frame = entry["target_frame"]
        if not isinstance(target_frame, int):
            raise LiveAdapterError(
                "frame-evidence-invalid", f"target frame not an int: {target_frame!r}"
            )
        comparison = compare_frames(ctx, before, after, target_frame)
        diff = comparison["mean_abs_diff"]
        if entry["already_applied"] and frame_changed(diff):
            raise LiveAdapterError(
                "frame-unstable-after-rerun",
                f"already-graded target {entry['item_id']} frame diff {diff}",
            )
        if not entry["already_applied"] and not frame_changed(diff):
            raise LiveAdapterError(
                "frame-unchanged",
                f"target {entry['item_id']} after-frame is unchanged (diff {diff}); "
                "an apply without a pixel change is not a success",
            )
        entry["frame"] = comparison


def _covering_span(
    frame: int, target_spans: tuple[tuple[int, int], ...]
) -> tuple[int, int] | None:
    for start, end in target_spans:
        if start <= frame < end:
            return (start, end)
    return None


def _is_native_cue_card(item: StructureItem, timeline_name: str) -> bool:
    """Conjunction of the live-observed native-cue-card identity fields
    (v44-real-01 structure readback): the overlay track the subtitle
    handler places cards on, no source file (a nested-timeline card
    renders internally), and the handler-minted card media-pool name.
    No single field qualifies an item — a span never does."""
    return (
        item.track_index == OVERLAY_TRACK_INDEX
        and item.file_path is None
        and is_cue_card_name(item.media_pool_item_name, timeline_name)
    )


def verify_untargeted_frames(  # noqa: PLR0913 (evidence gate carries its measured bindings)
    ctx: LiveSessionContext,
    snap: StructureSnapshot,
    target_ids: set[str],
    *,
    target_spans: tuple[tuple[int, int], ...],
    before: Path,
    after: Path,
) -> list[dict[str, object]]:
    """Every video item OUTSIDE the explicit targets must be frame-stable
    across the two renders (codec noise only). The only skip is an
    independently identified native subtitle cue card whose comparison
    frame lies INSIDE a targeted span (it composites the graded footage
    beneath it); every other untargeted item — including overlapping
    file-backed overlays — stays gated."""

    if ctx.timeline_start is None:  # pragma: no cover - resolve_targets enforced it first
        raise LiveAdapterError("timeline-not-prepared", "prepare first")
    rows: list[dict[str, object]] = []
    for item in video_items(snap):
        if item.timeline_item_id in target_ids:
            continue
        frame = (item.start or 0) + ((item.end or 1) - (item.start or 0)) // 2 - ctx.timeline_start
        covered = _covering_span(frame, target_spans)
        if covered is not None and _is_native_cue_card(item, snap.name):
            rows.append(
                {
                    "timeline_item_id": item.timeline_item_id,
                    "frame": frame,
                    "skipped": (
                        f"native subtitle cue card composited over targeted "
                        f"span [{covered[0]},{covered[1]})"
                    ),
                }
            )
            continue
        comparison = compare_frames(ctx, before, after, frame)
        if frame_changed(comparison["mean_abs_diff"]):
            raise LiveAdapterError(
                "untargeted-frame-changed",
                f"untargeted item {item.timeline_item_id} frame {frame} diff "
                f"{comparison['mean_abs_diff']} exceeded the codec-noise gate",
            )
        rows.append({"timeline_item_id": item.timeline_item_id, "frame": frame, **comparison})
    return rows


__all__ = [
    "FRAME_DIFF_GATE",
    "compare_frames",
    "frame_changed",
    "verify_target_frames",
    "verify_untargeted_frames",
]
