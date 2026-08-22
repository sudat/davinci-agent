"""Edit reconciliation of subtitle cues against Timeline IR v2 (task 33).

After the editorial edit, draft cues (Edit Source frames) are clipped and
shifted through the IR v2 PRIMARY video track placements — the A-roll edit is
what decides which speech survived. Mapping is the native-rate 1:1 placement
correspondence: ``record = placement.record_start + (source - placement.source_start)``.

Granularity contract (v1, documented): each cue maps through the SINGLE
placement whose source intersection with the cue is largest (ties broken by
earliest record position). A cue spanning two placements is trimmed to that
intersection with an explicit ``trimmed`` record; a cue with no surviving
overlap is ``dropped`` with a record. Non-chronological multi-placement spans
are intentionally not merged into one cue — merging across a re-ordered edit
would paste unrelated airtime into one subtitle.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from services.creative_plan.subtitle_models import (
    ReconciliationRecordV1,
    SubtitleDraftCueV1,
    SubtitleReconciledCueV1,
    SubtitleReconciliationV1,
)

if TYPE_CHECKING:
    from services.creative_plan.ir_models_v2 import TimelineIrV2


def reconcile_after_edit(
    cues: Sequence[SubtitleDraftCueV1], ir_v2: TimelineIrV2
) -> SubtitleReconciliationV1:
    """Clip/shift draft cues onto the IR v2 record spans; drop the cut ones."""
    primary = next(
        (track for track in ir_v2.video_tracks if track.role == "primary"),
        None,
    )
    kept: list[SubtitleReconciledCueV1] = []
    records: list[ReconciliationRecordV1] = []
    for cue in cues:
        overlaps: list[tuple[int, int, int, int, str]] = []
        if primary is not None:
            for item in primary.items:
                if item.source.source_id != cue.source_id:
                    continue
                start = max(cue.start_frame, item.source.span.start_frame)
                end = min(cue.end_frame, item.source.span.end_frame)
                if start >= end:
                    continue
                record_start = item.record_span.start_frame + (start - item.source.span.start_frame)
                overlaps.append(
                    (start, end, record_start, record_start + (end - start), item.item_id)
                )
        best = min(
            overlaps,
            key=lambda o: (-(o[1] - o[0]), o[2], o[4]),
            default=None,
        )
        if best is None:
            records.append(
                ReconciliationRecordV1(
                    cue_id=cue.cue_id,
                    transcript_ref=cue.transcript_ref,
                    action="dropped",
                    detail="source span was cut by the edit; no surviving placement overlap",
                    source_frames_lost=cue.end_frame - cue.start_frame,
                )
            )
            continue
        start, end, record_start, record_end, _item_id = best
        lost = (cue.end_frame - cue.start_frame) - (end - start)
        if lost > 0:
            records.append(
                ReconciliationRecordV1(
                    cue_id=cue.cue_id,
                    transcript_ref=cue.transcript_ref,
                    action="trimmed",
                    detail=(
                        f"partial placement overlap: kept source [{start},{end}) "
                        f"of [{cue.start_frame},{cue.end_frame})"
                    ),
                    source_frames_lost=lost,
                )
            )
        kept.append(
            SubtitleReconciledCueV1(
                cue_id=cue.cue_id,
                transcript_ref=cue.transcript_ref,
                source_id=cue.source_id,
                text=cue.text,
                start_frame=start,
                end_frame=end,
                record_start=record_start,
                record_end=record_end,
            )
        )
    return SubtitleReconciliationV1(cues=tuple(kept), records=tuple(records))


__all__ = ["reconcile_after_edit"]
