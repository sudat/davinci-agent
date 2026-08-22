"""PerformanceObservationV1 — audience-outcome observation artifact.

Separation contract: This module defines audience-outcome observations only
(views, CTR, retention, comment themes) and MUST NOT import, inherit from,
or share any model base with preference/taste evidence. Performance outcomes
never automatically rewrite operator taste; they are kept strictly separate
from preference evidence by schema and by import boundary. No symbol from
any taste or preference model may be imported or re-exported here.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, ValidationError

from services.contracts.primitives import Identifier, StrictModel


class RetentionPoint(StrictModel):
    """A notable retention timestamp with a human note."""

    timestamp_seconds: Annotated[float, Field(ge=0, strict=False)]
    note: Annotated[str, Field(min_length=1, strict=True)]


class CommentTheme(StrictModel):
    """An aggregated comment theme with frequency."""

    theme: Annotated[str, Field(min_length=1, strict=True)]
    frequency: Annotated[int, Field(ge=0, strict=True)]


class PerformanceObservationV1(StrictModel):
    """Audience-outcome observation for a single episode.

    Schema-only artifact; no metrics computation logic lives here.
    Distinguished by schema_version ``performance-observation-v1``.
    """

    schema_version: Literal["performance-observation-v1"] = "performance-observation-v1"
    episode_id: Identifier
    observed_at: Annotated[str, Field(min_length=1, strict=True)]
    views: Annotated[int, Field(ge=0, strict=True)]
    ctr: Annotated[float, Field(ge=0, le=1, strict=False)]
    average_view_duration_seconds: Annotated[float, Field(ge=0, strict=False)]
    average_percentage_viewed: Annotated[float, Field(ge=0, le=100, strict=False)]
    notable_retention_points: list[RetentionPoint] = Field(default_factory=list)
    comment_themes: list[CommentTheme] = Field(default_factory=list)


class PerformanceObservationValidationError(ValueError):
    """Typed error for manual import failures (bounds / unknown keys)."""


def import_manual(observation_dict: dict[str, object]) -> PerformanceObservationV1:
    """Validate a manual observation dict and return a typed artifact.

    Rejects unknown keys and malformed/bounds-violating values with a
    typed error (PerformanceObservationValidationError wrapping
    pydantic.ValidationError).
    """

    try:
        return PerformanceObservationV1.model_validate(observation_dict)
    except ValidationError as exc:
        raise PerformanceObservationValidationError(str(exc)) from exc
