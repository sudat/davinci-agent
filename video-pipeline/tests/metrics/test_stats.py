from __future__ import annotations

import pytest

from services.metrics.stats import (
    SMALL_N_THRESHOLD,
    TIME_METHODOLOGY,
    lower_median,
    nearest_rank_p90,
)


def test_lower_median_odd_n() -> None:
    assert lower_median([30, 10, 20]) == 20


def test_lower_median_even_n_picks_lower_middle() -> None:
    assert lower_median([10, 20, 30, 40]) == 20


def test_median_and_p90_single_sample() -> None:
    assert lower_median([123_456]) == 123_456
    assert nearest_rank_p90([123_456]) == 123_456


def test_p90_nearest_rank_known_vectors() -> None:
    assert nearest_rank_p90(list(range(1, 11))) == 9
    assert nearest_rank_p90([5, 1, 4, 2, 3]) == 5
    assert nearest_rank_p90(list(range(1, 21))) == 18


def test_empty_samples_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        lower_median([])
    with pytest.raises(ValueError, match="non-empty"):
        nearest_rank_p90(())


def test_small_n_honesty_constants() -> None:
    assert SMALL_N_THRESHOLD == 5
    assert "lower median" in TIME_METHODOLOGY
    assert "nearest-rank" in TIME_METHODOLOGY
    assert "sample_count" in TIME_METHODOLOGY
