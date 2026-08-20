"""Deterministic order statistics for time metrics.

Only pure selection statistics are used (no averaging), so every value
is an exact integer millisecond and canonical JSON never needs floats.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

SMALL_N_THRESHOLD: Final[int] = 5

TIME_METHODOLOGY: Final[str] = (
    "event-derived only (never wall-clock guesses); median is the lower median "
    "order statistic (even n keeps the lower middle; no averaging); P90 is the "
    "nearest-rank ceil(0.9*n) order statistic; integer milliseconds; every "
    "distribution carries sample_count and small_sample (n<"
    f"{SMALL_N_THRESHOLD}) honesty flags; missing data is not_evaluated with a "
    "reason, never zero"
)


def _rank(values: Sequence[int], rank: int) -> int:
    ordered = sorted(values)
    return ordered[rank - 1]


def lower_median(values: Sequence[int]) -> int:
    """Lower median: for even n the lower of the two middle values."""

    if not values:
        raise ValueError("lower_median requires non-empty samples")
    return _rank(values, (len(values) + 1) // 2)


def nearest_rank_p90(values: Sequence[int]) -> int:
    """Nearest-rank P90: the ceil(0.9*n)-th smallest value."""

    if not values:
        raise ValueError("nearest_rank_p90 requires non-empty samples")
    rank = -(-9 * len(values) // 10)  # ceil(0.9 * n) in exact integer math
    return _rank(values, rank)


__all__ = [
    "SMALL_N_THRESHOLD",
    "TIME_METHODOLOGY",
    "lower_median",
    "nearest_rank_p90",
]
