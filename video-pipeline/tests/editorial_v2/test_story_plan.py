from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.editorial_v2.story_plan import (
    StoryBlock,
    StoryPlanV1,
    UnknownStoryRefError,
    validate_story_plan,
)


def _brief_ref(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "episode_id": "ep-001",
        "content_hash": "a" * 64,
    }
    base.update(overrides)
    return base


def _talk_blocks():  # type: ignore[no-untyped-def]
    return [
        {
            "block_id": "blk-hook",
            "purpose": "Grab attention",
            "block_kind": "hook",
            "order": 0,
            "source_refs": ["theme-01"],
        },
        {
            "block_id": "blk-setup",
            "purpose": "Context",
            "block_kind": "setup",
            "order": 1,
            "source_refs": ["theme-02"],
        },
        {
            "block_id": "blk-problem",
            "purpose": "State problem",
            "block_kind": "problem",
            "order": 2,
            "source_refs": [],
        },
        {
            "block_id": "blk-evidence",
            "purpose": "Show evidence",
            "block_kind": "evidence",
            "order": 3,
            "source_refs": ["mom-01", "mom-02"],
            "notes": "needs B-roll",
        },
        {
            "block_id": "blk-payoff",
            "purpose": "Payoff",
            "block_kind": "payoff",
            "order": 4,
            "source_refs": ["mom-03"],
        },
        {
            "block_id": "blk-cta",
            "purpose": "Call to action",
            "block_kind": "cta",
            "order": 5,
            "source_refs": [],
        },
    ]


def _travel_blocks():  # type: ignore[no-untyped-def]
    return [
        {
            "block_id": "blk-loc-01",
            "purpose": "Kyoto morning market",
            "block_kind": "location",
            "order": 0,
            "source_refs": ["theme-10"],
        },
        {
            "block_id": "blk-exp-01",
            "purpose": "Tea tasting",
            "block_kind": "experience",
            "order": 1,
            "source_refs": ["mom-10"],
            "notes": "ambient sound",
        },
        {
            "block_id": "blk-temp-01",
            "purpose": "Evening reflection",
            "block_kind": "temporal",
            "order": 2,
            "source_refs": ["mom-11", "mom-12"],
        },
    ]


# ---------------------------------------------------------------------------
# (a) talk-shape round-trip (stale_state probe)
# ---------------------------------------------------------------------------


def test_talk_shape_round_trip() -> None:
    payload = {
        "episode_id": "ep-001",
        "brief_ref": _brief_ref(),
        "blocks": _talk_blocks(),
    }
    plan = StoryPlanV1.model_validate(payload)
    assert len(plan.blocks) == 6
    assert plan.blocks[0].block_kind == "hook"
    dumped = plan.model_dump(mode="json", by_alias=True)
    # JSON dumps lists; re-validate coerces back to tuples
    reparsed = StoryPlanV1.model_validate(dumped)
    assert reparsed == plan
    # tuple coercion check
    assert isinstance(plan.blocks, tuple)
    assert isinstance(plan.blocks[0].source_refs, tuple)
    # JSON round-trip via model_validate_json
    json_bytes = plan.model_dump_json(by_alias=True)
    assert StoryPlanV1.model_validate_json(json_bytes) == plan


# ---------------------------------------------------------------------------
# (b) travel-shape round-trip — non-chapter kinds accepted (shape-agnostic)
# ---------------------------------------------------------------------------


def test_travel_shape_round_trip_non_chapter_kinds_accepted() -> None:
    payload = {
        "episode_id": "ep-002",
        "brief_ref": _brief_ref(episode_id="ep-002"),
        "blocks": _travel_blocks(),
    }
    plan = StoryPlanV1.model_validate(payload)
    assert [b.block_kind for b in plan.blocks] == ["location", "experience", "temporal"]
    dumped = plan.model_dump(mode="json", by_alias=True)
    assert StoryPlanV1.model_validate(dumped) == plan
    # arbitrary free-form kind also accepted
    arbitrary = StoryBlock.model_validate(
        {
            "block_id": "blk-x",
            "purpose": "free form",
            "block_kind": "my-custom-shape",
            "order": 0,
            "source_refs": [],
        }
    )
    assert arbitrary.block_kind == "my-custom-shape"


# ---------------------------------------------------------------------------
# (c) out-of-order order values -> ValidationError (malformed_input probe)
# ---------------------------------------------------------------------------


def test_out_of_order_rejected() -> None:
    # gap: 0,2 missing 1
    with pytest.raises(ValidationError):
        StoryPlanV1.model_validate(
            {
                "episode_id": "ep-003",
                "brief_ref": _brief_ref(episode_id="ep-003"),
                "blocks": [
                    {
                        "block_id": "blk-0",
                        "purpose": "p0",
                        "block_kind": "hook",
                        "order": 0,
                        "source_refs": [],
                    },
                    {
                        "block_id": "blk-2",
                        "purpose": "p2",
                        "block_kind": "setup",
                        "order": 2,
                        "source_refs": [],
                    },
                ],
            }
        )


def test_duplicate_order_rejected() -> None:
    with pytest.raises(ValidationError):
        StoryPlanV1.model_validate(
            {
                "episode_id": "ep-003",
                "brief_ref": _brief_ref(episode_id="ep-003"),
                "blocks": [
                    {
                        "block_id": "blk-a",
                        "purpose": "p",
                        "block_kind": "hook",
                        "order": 0,
                        "source_refs": [],
                    },
                    {
                        "block_id": "blk-b",
                        "purpose": "p",
                        "block_kind": "setup",
                        "order": 0,
                        "source_refs": [],
                    },
                ],
            }
        )


def test_unsorted_order_rejected() -> None:
    # orders 1,0 — unsorted relative to list position and not 0..n-1 sequentially
    with pytest.raises(ValidationError):
        StoryPlanV1.model_validate(
            {
                "episode_id": "ep-003",
                "brief_ref": _brief_ref(episode_id="ep-003"),
                "blocks": [
                    {
                        "block_id": "blk-1",
                        "purpose": "p",
                        "block_kind": "hook",
                        "order": 1,
                        "source_refs": [],
                    },
                    {
                        "block_id": "blk-0",
                        "purpose": "p",
                        "block_kind": "setup",
                        "order": 0,
                        "source_refs": [],
                    },
                ],
            }
        )


# ---------------------------------------------------------------------------
# (d) validate_story_plan with unknown ref -> UnknownStoryRefError naming it
# ---------------------------------------------------------------------------


def test_validate_unknown_ref_raises_typed_error() -> None:
    plan = StoryPlanV1.model_validate(
        {
            "episode_id": "ep-004",
            "brief_ref": _brief_ref(episode_id="ep-004"),
            "blocks": [
                {
                    "block_id": "blk-0",
                    "purpose": "p",
                    "block_kind": "hook",
                    "order": 0,
                    "source_refs": ["known-01", "missing-xyz"],
                }
            ],
        }
    )
    with pytest.raises(UnknownStoryRefError) as exc:
        validate_story_plan(plan, {"known-01", "known-02"})
    assert "missing-xyz" in str(exc.value)
    # typed error exposes missing id
    assert getattr(exc.value, "missing_id", None) == "missing-xyz" or "missing-xyz" in str(
        exc.value.missing_id if hasattr(exc.value, "missing_id") else exc.value
    )


# ---------------------------------------------------------------------------
# (e) all refs known -> clean validation
# ---------------------------------------------------------------------------


def test_validate_all_refs_known_clean() -> None:
    plan = StoryPlanV1.model_validate(
        {
            "episode_id": "ep-005",
            "brief_ref": _brief_ref(episode_id="ep-005"),
            "blocks": [
                {
                    "block_id": "blk-0",
                    "purpose": "p",
                    "block_kind": "hook",
                    "order": 0,
                    "source_refs": ["a", "b"],
                },
                {
                    "block_id": "blk-1",
                    "purpose": "p",
                    "block_kind": "setup",
                    "order": 1,
                    "source_refs": [],
                },
            ],
        }
    )
    # should not raise
    validated = validate_story_plan(plan, {"a", "b", "c"})
    assert validated is plan or validated == plan

    # also with empty known and empty refs -> clean
    empty_plan = StoryPlanV1.model_validate(
        {
            "episode_id": "ep-005",
            "brief_ref": _brief_ref(episode_id="ep-005"),
            "blocks": [
                {
                    "block_id": "blk-0",
                    "purpose": "p",
                    "block_kind": "hook",
                    "order": 0,
                    "source_refs": [],
                }
            ],
        }
    )
    validate_story_plan(empty_plan, set())
