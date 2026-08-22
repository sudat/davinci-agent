"""StoryPlanV1 — structure-first story plan (task 21).

Shape-agnostic: ``block_kind`` is free-form (hook/setup/.../cta allowed but
location/experience/temporal equally valid; no chapter-model enforcement).
Order is contiguous 0..n-1 and enforces sequential validation. Reference
existence is checked via ``validate_story_plan`` against a supplied index.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel


def _to_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


# ---------------------------------------------------------------------------
# Typed error for unknown ref
# ---------------------------------------------------------------------------


class UnknownStoryRefError(ValueError):
    """Raised when a StoryBlock source_ref is not in the supplied index."""

    def __init__(self, code: str, detail: str, missing_id: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.missing_id = missing_id


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class StoryBriefRef(StrictModel):
    """Reference to the approved EpisodeBriefV1.

    Minimal purpose-bound reference: captures the brief's episode scope and
    optional content hash for audit correlation. ``episode_id`` aligns with
    ``StoryPlanV1.episode_id``.
    """

    episode_id: Identifier
    content_hash: Sha256 | None = None


class StoryBlock(StrictModel):
    """One structural block in the story plan."""

    block_id: Identifier
    purpose: Annotated[str, Field(min_length=1, strict=True)]
    block_kind: Annotated[str, Field(min_length=1, strict=True)]
    order: Annotated[int, Field(ge=0, strict=True)]
    source_refs: Annotated[
        tuple[Identifier, ...], BeforeValidator(_to_tuple)
    ] = Field(default_factory=tuple)
    notes: Annotated[str, Field(min_length=1, strict=True)] | None = None


# ---------------------------------------------------------------------------
# Main artifact
# ---------------------------------------------------------------------------


class StoryPlanV1(StrictModel):
    schema_version: Literal["story-plan-v1"] = "story-plan-v1"
    episode_id: Identifier
    brief_ref: StoryBriefRef
    blocks: Annotated[
        tuple[StoryBlock, ...], BeforeValidator(_to_tuple)
    ] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_blocks(self) -> StoryPlanV1:
        # Enforce sequential 0..n-1 contiguous ordering. Each block's order
        # must equal its index when blocks are considered; also enforce
        # sorted-by-order and no duplicates/gaps.
        orders = [b.order for b in self.blocks]
        # Must be sorted ascending (input order dictates narrative order)
        if orders != sorted(orders):
            raise PydanticCustomError(
                "story_order_unsorted",
                "blocks must be sorted by order ascending",
            )
        # Must be contiguous 0..n-1
        expected = list(range(len(self.blocks)))
        if orders != expected:
            raise PydanticCustomError(
                "story_order_gap",
                "block order must be contiguous 0..n-1 without gaps or duplicates",
            )
        # Optional cross-check: brief_ref episode scope aligns with plan episode
        if self.brief_ref.episode_id != self.episode_id:
            raise PydanticCustomError(
                "brief_episode_mismatch",
                "brief_ref.episode_id must match StoryPlanV1.episode_id",
            )
        return self


# ---------------------------------------------------------------------------
# Reference existence validation (separate from schema; uses supplied index)
# ---------------------------------------------------------------------------


def validate_story_plan(
    plan: StoryPlanV1,
    known_ids: set[Identifier] | set[str],
) -> StoryPlanV1:
    """Validate that every block source_ref exists in ``known_ids``.

    Raises:
        UnknownStoryRefError: naming the first missing id.
    """

    for block in plan.blocks:
        for ref in block.source_refs:
            if ref not in known_ids:
                raise UnknownStoryRefError(
                    "unknown-story-ref",
                    f"source_ref '{ref}' not in known_ids (block {block.block_id})",
                    missing_id=ref,
                )
    return plan


__all__ = [
    "StoryBlock",
    "StoryBriefRef",
    "StoryPlanV1",
    "UnknownStoryRefError",
    "validate_story_plan",
]
