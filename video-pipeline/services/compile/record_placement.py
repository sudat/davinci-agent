"""Deterministic record placement and cue coverage mapping (Todo 44).

Placement is cursor-verified per logical track: record holes become explicit
gap ranges (legal), overlapping record spans are typed errors. Transcript cue
spans map onto the timeline through the placed video items' source→record
correspondence — coverage outside kept items produces nothing (removed
dialogue gets no cues) and a cue crossing an item boundary splits at the
exact integer boundary frames.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

from services.compile.production_errors import CompileProductionError

if TYPE_CHECKING:
    from services.compile.conform_inputs import ResolvedCue

TrackKindAv = Literal["video", "audio"]


@dataclass(frozen=True, slots=True)
class AvPlacement:
    """One A/V item placement: source span, record span, link group."""

    item_id: str
    track_kind: TrackKindAv
    source_id: str
    source_start: int
    source_end: int
    record_start: int
    record_end: int
    av_link_id: str


@dataclass(frozen=True, slots=True)
class TrackAllocation:
    """Cursor-verified placements plus the explicit gap ranges."""

    items: tuple[AvPlacement, ...]
    gaps: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class CuePiece:
    """One cue (piece) resolved to concrete source and record spans."""

    segment_id: str
    text: str
    source_start: int
    source_end: int
    record_start: int
    record_end: int


def allocate_track(kind: str, items: Iterable[AvPlacement]) -> TrackAllocation:
    """Verify placements per track: holes -> gaps, overlaps -> typed error."""

    ordered = sorted(items, key=lambda item: (item.record_start, item.item_id))
    placed: list[AvPlacement] = []
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for item in ordered:
        if item.record_start < cursor:
            raise CompileProductionError(
                "record_overlap",
                f"{kind} items {placed[-1].item_id} and {item.item_id} overlap at "
                f"record frame {item.record_start}",
            )
        if item.record_start > cursor:
            gaps.append((cursor, item.record_start))
        placed.append(item)
        cursor = item.record_end
    return TrackAllocation(items=tuple(placed), gaps=tuple(gaps))


def cue_pieces(
    video: Iterable[AvPlacement], cues: Iterable[ResolvedCue]
) -> tuple[CuePiece, ...]:
    """Map cue spans through kept video items: filter, split at boundaries."""

    kept = sorted(video, key=lambda item: (item.record_start, item.item_id))
    pieces: list[CuePiece] = []
    for cue in cues:
        for item in kept:
            start = max(cue.start_frame, item.source_start)
            end = min(cue.end_frame, item.source_end)
            if start >= end:
                continue
            pieces.append(
                CuePiece(
                    segment_id=cue.segment_id,
                    text=cue.text,
                    source_start=start,
                    source_end=end,
                    record_start=item.record_start + start - item.source_start,
                    record_end=item.record_start + end - item.source_start,
                )
            )
    pieces.sort(key=lambda piece: (piece.record_start, piece.record_end, piece.segment_id))
    for previous, current in pairwise(pieces):
        if current.record_start < previous.record_end:
            raise CompileProductionError(
                "record_overlap",
                f"subtitle cues {previous.segment_id} and {current.segment_id} "
                f"overlap at record frame {current.record_start}",
            )
    return tuple(pieces)


__all__ = [
    "AvPlacement",
    "CuePiece",
    "TrackAllocation",
    "TrackKindAv",
    "allocate_track",
    "cue_pieces",
]
