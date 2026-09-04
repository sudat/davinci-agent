"""Deterministic merge of contiguous A/V placements (compile-time transform).

Speech-granular plans place one item per allocated speech candidate; when
adjacent candidates read physically-continuous source frames back-to-back
on the record timeline, the split is pure editor noise (measured on
v44-real-01: 97 items, only 5 real source jumps). Merging here joins
those neighbors into single timeline clips at compile time — the committed
plan is never rewritten; record content, frame mapping, and subtitle cue
spans are invariant by construction (a merged span is the union of its
parts, and cue mapping works on source-span overlap).
"""

from __future__ import annotations

from typing import Final

from services.compile.record_placement import AvPlacement

_MERGED_SEPARATOR: Final = "x"


def _mergeable(
    previous: AvPlacement, current: AvPlacement, split_frames: frozenset[int]
) -> bool:
    if previous.track_kind != current.track_kind:
        return False
    if previous.source_id != current.source_id:
        return False
    if previous.record_end != current.record_start:
        return False
    if previous.source_end != current.source_start:
        return False
    return not (split_frames & _span_frames(previous.record_start, current.record_end))


def _span_frames(start: int, end: int) -> frozenset[int]:
    return frozenset(range(start + 1, end))


def _merged_id(first_id: str, count: int) -> str:
    if count <= 1:
        return first_id
    return f"merged.{first_id}{_MERGED_SEPARATOR}{count}"


def merge_contiguous(
    placements: tuple[AvPlacement, ...],
    *,
    split_at_record_frames: tuple[int, ...] = (),
) -> tuple[AvPlacement, ...]:
    """Merge record-and-source-contiguous neighbors into single placements.

    Deterministic, total, pure. Two adjacent items (sorted by record start)
    merge when they share track kind + source id, the previous end equals
    the next start on BOTH the record and source timelines, and no explicit
    split frame falls strictly inside the joined record span. The merged
    item carries the first member's item_id/av_link_id (deterministic
    representative); the id records the member count for traceability.
    Callers that need the original member ids (e.g. to re-derive color
    targets) read them from the committed plan — this transform never
    rewrites it.
    """
    split = frozenset(split_at_record_frames)
    ordered = sorted(placements, key=lambda p: (p.track_kind, p.record_start, p.item_id))
    result: list[AvPlacement] = []
    for placement in ordered:
        if result:
            previous = result[-1]
            if previous.item_id.startswith("merged.") and _mergeable(
                _unmerged_head(previous), placement, split
            ):
                result[-1] = _absorb(previous, placement)
                continue
            if _mergeable(previous, placement, split):
                result[-1] = _start_merge(previous, placement)
                continue
        result.append(placement)
    return tuple(result)


def _start_merge(head: AvPlacement, tail: AvPlacement) -> AvPlacement:
    return AvPlacement(
        item_id=_merged_id(head.item_id, 2),
        track_kind=head.track_kind,
        source_id=head.source_id,
        source_start=head.source_start,
        source_end=tail.source_end,
        record_start=head.record_start,
        record_end=tail.record_end,
        av_link_id=head.av_link_id,
    )


def _absorb(merged: AvPlacement, tail: AvPlacement) -> AvPlacement:
    count = int(merged.item_id.rsplit(_MERGED_SEPARATOR, 1)[1]) + 1
    head_id = merged.item_id.rsplit(_MERGED_SEPARATOR, 1)[0].removeprefix("merged.")
    return AvPlacement(
        item_id=_merged_id(head_id, count),
        track_kind=merged.track_kind,
        source_id=merged.source_id,
        source_start=merged.source_start,
        source_end=tail.source_end,
        record_start=merged.record_start,
        record_end=tail.record_end,
        av_link_id=merged.av_link_id,
    )


def _unmerged_head(merged: AvPlacement) -> AvPlacement:
    return merged


__all__ = ["merge_contiguous"]
