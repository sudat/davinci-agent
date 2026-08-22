"""PublishabilityReviewV1 sparse-input and trend aggregation tests (TDD red)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.final_review.publishability import (
    PublishabilityReviewV1,
    aggregate_trend,
    parse_publishability_input,
)


def _sparse_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": "publishability-review-v1",
        "episode_id": "ep-001",
        "run_id": "run-001",
        "publishable": "as_is",
    }
    base.update(overrides)
    return base


def test_minimal_sparse_input_parses_ok() -> None:
    review = PublishabilityReviewV1.model_validate(_sparse_payload())
    assert review.publishable == "as_is"
    assert review.episode_id == "ep-001"
    assert review.run_id == "run-001"
    assert review.overall_comment is None
    assert review.best_timestamp is None
    assert review.worst_timestamp is None
    assert review.domain_ratings is None
    assert review.ab_comparison is None


def test_invalid_enum_rejected() -> None:
    with pytest.raises((ValidationError, ValueError, TypeError)):
        parse_publishability_input(_sparse_payload(publishable="maybe"))


def test_domain_rating_bounds_rejected() -> None:
    with pytest.raises((ValidationError, ValueError, TypeError)):
        parse_publishability_input(
            _sparse_payload(domain_ratings={"pacing": 6}),
        )


def test_trend_improving_direction() -> None:
    r1 = PublishabilityReviewV1.model_validate(
        _sparse_payload(episode_id="ep-001", run_id="run-001", publishable="not_yet"),
    )
    r2 = PublishabilityReviewV1.model_validate(
        _sparse_payload(
            episode_id="ep-001", run_id="run-002", publishable="after_small_corrections"
        ),
    )
    r3 = PublishabilityReviewV1.model_validate(
        _sparse_payload(episode_id="ep-001", run_id="run-003", publishable="as_is"),
    )
    trend = aggregate_trend([("run-001", r1), ("run-002", r2), ("run-003", r3)])
    assert trend.runs_compared == 3
    assert trend.direction == "improving"
    assert 0 <= trend.publishable_rate <= 1


def test_trend_insufficient_data_single_run() -> None:
    r1 = PublishabilityReviewV1.model_validate(_sparse_payload())
    trend = aggregate_trend([("run-001", r1)])
    assert trend.runs_compared == 1
    assert trend.direction == "insufficient_data"


def test_round_trip() -> None:
    original = PublishabilityReviewV1.model_validate(
        {
            "schema_version": "publishability-review-v1",
            "episode_id": "ep-001",
            "run_id": "run-001",
            "publishable": "after_small_corrections",
            "overall_comment": "needs tighter pacing in middle",
            "best_timestamp": {"ts_seconds": 42.5, "note": "great energy"},
            "worst_timestamp": {"ts_seconds": 120.0, "note": "dragging segment"},
            "domain_ratings": {"pacing": 3, "audio": 4},
            "ab_comparison": {"preferred": "a", "note": "A feels more natural"},
        }
    )
    payload = original.model_dump(mode="json")
    restored = PublishabilityReviewV1.model_validate(payload)
    assert restored == original
    via_parse = parse_publishability_input(payload)
    assert via_parse == original
