"""Bounded placement reconciliation (split from ``placement.py``).

The verification cluster behind idempotent placement: ONE bounded per-track
``get_items_in_track`` scan (session-scoped snapshot) plus the targeted
per-item ``timeline_item.get_source_*_frame`` readbacks, and the measured
tolerance rules that decide presence. The whole-timeline
``source_range_report`` walk is never issued here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from services.mcp_client.ops_models import SourceFrameResult, TrackItemsResult
from services.mcp_execution.live_errors import LiveAdapterError
from services.mcp_execution.live_handlers.common import require_ok

if TYPE_CHECKING:
    from services.mcp_client.ops_models import TrackItemRow
    from services.mcp_execution.live_handlers.common import LiveSessionContext

#: Product placements always land on track 1 of their track type (the
#: append payload pins it) — the single addressing authority for the
#: bounded reconciliation readback.
PLACEMENT_TRACK_INDEX: Final = 1

#: Measured deadline for ONE bounded per-track scan and the targeted
#: per-item source-frame readbacks (live-measured 2026-08-28: 119.8 s /
#: 97 items; RE-MEASURED 2026-08-29 fresh: 292.6 s / 97 items on video
#: track 1 — same server-state growth that pushed track-2 to 336.5 s and
#: structure walks past 1500 s; the whole-timeline source_range_report
#: walk (294 occurrences) stopped completing even at 900 s — placement
#: must not issue it). Ceiling 300 → 600 s (~2x over fresh 292.6 s).
PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS: Final = 600.0

SOURCE_READBACK_TOLERANCE_FRAMES: Final = 1

#: Measured Resolve 21.0.4.5 behavior (2026-08-29; full finishing runs r3/r4
#: plus fresh-session targeted readbacks on v44-real-01): an item whose
#: committed source span ends exactly at the imported media's frame EOF
#: (ffprobe nb_frames) reads its source END back this many frames short —
#: [8388,8465) against committed [8388,8467) — on BOTH the video and audio
#: tracks while interior items stay exact. The same verification passed
#: within ±1 on 2026-08-27 with no placement mutation, so the READBACK
#: drifted, not the timeline. Accepted ONLY when the committed end equals
#: the independently established media EOF (ctx.media_frame_counts) and
#: only for exactly this shortfall; every other shape keeps the rule above.
MEDIA_EOF_END_DRIFT_FRAMES: Final = 2


def _track_rows(ctx: LiveSessionContext, track_type: str) -> tuple[TrackItemRow, ...]:
    """Bounded per-track scan (session-scoped until any content mutation):
    ONE get_items_in_track walk covers every placement on the track —
    rows carry the ABSOLUTE record span; source spans are verified per
    item through the targeted frame readback, never assumed from rows."""
    snapshot = ctx.placement_scan.get(track_type)
    if snapshot is None:
        snapshot = TrackItemsResult.model_validate(
            ctx.transport(
                "timeline",
                "get_items_in_track",
                {"track_type": track_type, "track_index": PLACEMENT_TRACK_INDEX},
                timeout_seconds=PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS,
            )
        )
        require_ok(snapshot, "get-items-in-track")
        ctx.placement_scan[track_type] = snapshot
    return snapshot.items


def _source_frames(
    ctx: LiveSessionContext, track_type: str, item_index: int
) -> tuple[int, int]:
    """Targeted independent source-span readback for ONE item, addressed
    by its position in the same track list the bounded scan read. Both
    vendor actions are written literally on purpose: the dispositions
    route-binding verifier re-derives literal (tool, action) pairs from
    this source."""
    address: dict[str, object] = {
        "track_type": track_type,
        "track_index": PLACEMENT_TRACK_INDEX,
        "item_index": item_index,
    }
    start = SourceFrameResult.model_validate(
        ctx.transport(
            "timeline_item",
            "get_source_start_frame",
            address,
            timeout_seconds=PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS,
        )
    )
    require_ok(start, "get-source-start-frame")
    end = SourceFrameResult.model_validate(
        ctx.transport(
            "timeline_item",
            "get_source_end_frame",
            address,
            timeout_seconds=PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS,
        )
    )
    require_ok(end, "get-source-end-frame")
    if start.frame is None or end.frame is None:
        raise LiveAdapterError(
            "source-frame-readback-missing", f"source frames item {item_index} on {track_type}"
        )
    return start.frame, end.frame


def _source_end_matches(
    read_end: int, expected_end: int, media_eof: int | None
) -> bool:
    if abs(read_end - expected_end) <= SOURCE_READBACK_TOLERANCE_FRAMES:
        return True
    return (
        media_eof is not None
        and expected_end == media_eof
        and expected_end - read_end == MEDIA_EOF_END_DRIFT_FRAMES
    )


def _placement_present(  # noqa: PLR0913, PLR0917 (ctx+track+two span tuples+EOF is the irreducible reconciliation signature)
    ctx: LiveSessionContext,
    track_type: str,
    rows: tuple[TrackItemRow, ...],
    source_range: tuple[int, int],
    timeline_range: tuple[int, int],
    media_eof: int | None,
) -> bool:
    """Independent presence proof on the placement's own track: the scan is
    addressed BY track type (an item it returns IS on that track type), the
    record span must match EXACTLY, and the source span is read back per
    candidate item. Vendor truth (probe-evidenced): GetSourceStartFrame may
    read one frame early or late, so the START bound tolerates exactly 1
    frame; the END bound tolerates 1 frame generally, plus the measured
    media-EOF two-frame shortfall (:data:`MEDIA_EOF_END_DRIFT_FRAMES`) when
    the committed end equals the trusted media EOF. Record positions are
    exact and any difference there is a mismatch."""
    src_s, src_e = source_range
    abs_start, abs_end = timeline_range
    for index, row in enumerate(rows):
        if row.start != abs_start or row.end != abs_end:
            continue
        src_start, src_end = _source_frames(ctx, track_type, index)
        within_source_tolerance = (
            abs(src_start - src_s) <= SOURCE_READBACK_TOLERANCE_FRAMES
            and _source_end_matches(src_end, src_e, media_eof)
        )
        if within_source_tolerance:
            return True
    return False


__all__ = [
    "MEDIA_EOF_END_DRIFT_FRAMES",
    "PLACEMENT_TRACK_INDEX",
    "PLACEMENT_TRACK_SCAN_TIMEOUT_SECONDS",
    "SOURCE_READBACK_TOLERANCE_FRAMES",
    "_placement_present",
    "_track_rows",
]
