"""WBS-2 persistent-span tile boundary table (DESIGN telop-nested §2.4).

The exact expected spans for the representative 30fps episode line:
persistent [90,7837) tiled at the WBS-1 measured 150-frame card length,
with NO tiles during the chapter-card gap [1632,1677) (Opus-confirmed
「消す」 and the measured same-track overlap refusal).
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from services.mcp_execution.live_handlers.telop_spans import (
    TELOP_TILE_FRAMES,
    tile_spans,
)

#: The chapter gap on the representative episode line (frozen numbers from
#: ``services/cli/_v44_chapter_card_plan.py``: RECORD_FRAME=1632, 45 frames).
CHAPTER_GAP = (1632, 1677)

#: Run 1 [90,1632): 1542 frames → ten full 150-frame tiles + one 42-frame
#: tail clipped at the gap edge. Literal table — the regression guard for
#: off-by-one tile math.
RUN1 = (
    (90, 240),
    (240, 390),
    (390, 540),
    (540, 690),
    (690, 840),
    (840, 990),
    (990, 1140),
    (1140, 1290),
    (1290, 1440),
    (1440, 1590),
    (1590, 1632),
)

#: Run 2 [1677,7837): 6160 frames → forty-one full tiles + one 10-frame tail.
RUN2_HEAD = ((1677, 1827), (1827, 1977))
RUN2_TAIL = ((7677, 7827), (7827, 7837))
RUN2_COUNT = 42


def test_tile_frames_constant_is_the_measured_probe_value() -> None:
    # Given: the WBS-1 probe check-a measurement (insert-title default on a
    #        30fps timeline is 150 frames)
    # Then: the handler's tile length is exactly that measured value
    assert TELOP_TILE_FRAMES == 150


def test_representative_persistent_span_tiling_is_the_exact_boundary_table() -> None:
    # Given: the persistent span [90,7837) with the chapter gap [1632,1677)
    # When: computing tiles at the measured 150-frame length
    # Then: run 1 matches the literal table (last tile clipped at the gap)
    spans = tile_spans((90, 7837), (CHAPTER_GAP,), TELOP_TILE_FRAMES)
    assert spans[: len(RUN1)] == RUN1
    # And: run 2 has the exact count, its head, and its clipped tail
    run2 = spans[len(RUN1) :]
    assert len(run2) == RUN2_COUNT
    assert run2[:2] == RUN2_HEAD
    assert run2[-2:] == RUN2_TAIL
    # And: the union covers the persistent span minus the gap, contiguously
    # inside each run, with no tile entering the gap
    assert spans[0][0] == 90
    assert spans[-1][1] == 7837
    for (_, end), (start, _) in pairwise(spans):
        if start != end:
            assert (end, start) == CHAPTER_GAP
    for start, end in spans:
        assert not (start < CHAPTER_GAP[1] and CHAPTER_GAP[0] < end)


def test_total_tile_count_matches_the_design_estimate() -> None:
    # Given: the representative line
    # When: the tiles are computed
    # Then: 11 + 42 = 53 tiles (DESIGN §2.4 "約52枚" estimate)
    assert len(tile_spans((90, 7837), (CHAPTER_GAP,), TELOP_TILE_FRAMES)) == 53


def test_no_gap_tiles_the_whole_span() -> None:
    # Given: a persistent span with no chapter card
    # When: the tiles are computed
    # Then: one contiguous run covering the whole span
    spans = tile_spans((0, 320), (), 150)
    assert spans == ((0, 150), (150, 300), (300, 320))


def test_exact_multiple_and_short_spans_produce_no_empty_tail() -> None:
    # Given: a span that is an exact multiple of the tile length
    # When: the tiles are computed
    # Then: full tiles only (no zero-length trailing tile)
    assert tile_spans((0, 300), (), 150) == ((0, 150), (150, 300))
    # And: a span shorter than one tile becomes a single clipped tile
    assert tile_spans((90, 120), (), 150) == ((90, 120),)


def test_gap_clipping_and_full_coverage_edge_cases() -> None:
    # Given: a gap that swallows a whole run boundary and one that covers
    #        the entire span
    # When: the tiles are computed
    # Then: tiles never overlap the gap and the surrounding runs survive
    assert tile_spans((0, 400), ((150, 300),), 150) == ((0, 150), (300, 400))
    assert tile_spans((0, 400), ((0, 400),), 150) == ()
    # A gap half-outside the span clips to the intersection only
    assert tile_spans((100, 400), ((350, 500),), 150) == (
        (100, 250),
        (250, 350),
    )


def test_invalid_arguments_refuse_typed() -> None:
    # Given: a non-positive tile length or an inverted span
    # When: the tiles are computed
    # Then: ValueError — never an infinite or negative-progress loop
    with pytest.raises(ValueError, match="tile"):
        tile_spans((0, 100), (), 0)
    with pytest.raises(ValueError, match="span"):
        tile_spans((100, 90), (), 150)
