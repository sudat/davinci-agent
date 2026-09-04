"""Pure span math for telop placement (no vendor calls).

Interval arithmetic only: tiling a persistent span at the measured card
length with chapter-gap clipping, grouping tiles into contiguous runs for
batched appends, and the fresh-scan span/overlap proof. Kept free of the
shared session context so the boundary table is unit-testable without
any transport.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, Final

from services.mcp_execution.live_handlers.common import LiveAdapterError

if TYPE_CHECKING:
    from collections.abc import Sequence

#: WBS-1 probe check-a: ``insert_fusion_title`` default duration on a
#: 30fps timeline is 150 frames (5 s default scales with the rate — 120
#: at 24fps). This is the persistent-span tile length on the 30fps line.
TELOP_TILE_FRAMES: Final = 150


def tile_spans(
    span: tuple[int, int],
    gaps: tuple[tuple[int, int], ...],
    tile_frames: int,
) -> tuple[tuple[int, int], ...]:
    """Tile ``span`` with half-open tiles of ``tile_frames`` (last tile
    clipped), skipping every gap interval (chapter-card periods carry no
    persistent tiles — measured same-track overlap refusal, DESIGN §7.6)."""
    start, end = span
    if tile_frames <= 0:
        raise ValueError(f"tile_frames must be positive, got {tile_frames}")
    if end <= start:
        raise ValueError(f"span [{start},{end}) must be non-empty")
    runs: list[tuple[int, int]] = []
    cursor = start
    for gap_start, gap_end in sorted(gaps):
        clip_start = max(cursor, gap_start)
        clip_end = min(end, gap_end)
        if clip_start < clip_end:
            runs.append((cursor, clip_start))
            cursor = clip_end
    runs.append((cursor, end))
    spans: list[tuple[int, int]] = []
    for run_start, run_end in runs:
        cursor = run_start
        while cursor < run_end:
            spans.append((cursor, min(cursor + tile_frames, run_end)))
            cursor += tile_frames
    return tuple(spans)


def contiguous_runs(spans: Sequence[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """Group sorted spans into maximal adjacent runs (tile butt joints)."""
    runs: list[list[tuple[int, int]]] = []
    for span in sorted(spans):
        if runs and span[0] == runs[-1][-1][1]:
            runs[-1].append(span)
        else:
            runs.append([span])
    return runs


def verify_track_spans(
    scanned: Sequence[tuple[int | None, int | None]], expected: tuple[tuple[int, int], ...]
) -> None:
    """Fresh-scan proof: every expected half-open span exists and the track
    rows never overlap (sorted adjacency; end exclusive)."""
    rows = [
        (start, end) for start, end in scanned if start is not None and end is not None
    ]
    present = set(rows)
    for span in expected:
        if span not in present:
            raise LiveAdapterError(
                "telop-span-mismatch",
                f"no telop item at record [{span[0]},{span[1]})",
            )
    ordered = sorted(rows)
    for (_, prev_end), (next_start, _) in pairwise(ordered):
        if prev_end > next_start:
            raise LiveAdapterError(
                "telop-track-overlap",
                f"telop track items overlap at [{next_start},{prev_end})",
            )


__all__ = [
    "TELOP_TILE_FRAMES",
    "contiguous_runs",
    "tile_spans",
    "verify_track_spans",
]
