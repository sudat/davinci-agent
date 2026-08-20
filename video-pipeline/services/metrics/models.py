"""Bundle input models: episode declarations, timing events, KPI claims.

PRD 22.4: without a dedicated Review UI, Active Human Time is estimated
from recorded Job-Log events (session start/end, review markers,
correction instructions, approvals) plus explicitly flagged self-report.
A missing timestamp is recorded data, never interpolated.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel

type Seq[Value] = Annotated[tuple[Value, ...], BeforeValidator(tuple)]

EpisodeKind = Literal["real", "technical", "synthetic"]
TimingPhase = Literal["session", "review", "correction", "qc", "final"]

TIMING_PHASES: Final[tuple[TimingPhase, ...]] = (
    "session",
    "review",
    "correction",
    "qc",
    "final",
)
ACTIVE_HUMAN_TIME: Final = "active_human_time"

ClaimMetricName = Literal[
    "active_human_time_median_ms",
    "active_human_time_p90_ms",
    "editorial_first_pass_rate_percent",
    "supported_episode_coverage_ratio_percent",
]


class EpisodeDeclaration(StrictModel):
    """One episode's standing in the metrics denominator.

    ``episode_kind`` real/technical/synthetic is the honesty label:
    technical and synthetic fixtures are NEVER eligible for human-time
    KPI claims. ``in_contract=False`` episodes are excluded from the KPI
    denominator and must state why (a hidden exclusion is a typed
    validation failure, never a silent drop).
    """

    episode_id: Identifier
    episode_kind: EpisodeKind
    in_contract: bool
    exclusion_reason: str | None = Field(default=None, min_length=1, strict=True)
    editorial_approval_record_id: Identifier | None = None

    @model_validator(mode="after")
    def require_reason_iff_excluded(self) -> EpisodeDeclaration:
        if not self.in_contract and self.exclusion_reason is None:
            raise PydanticCustomError(
                "exclusion_reason_missing",
                "out-of-contract episodes must state an exclusion reason "
                "(hiding episodes from the KPI denominator is forbidden)",
            )
        if self.in_contract and self.exclusion_reason is not None:
            raise PydanticCustomError(
                "exclusion_reason_forbidden",
                "in-contract episodes carry no exclusion reason",
            )
        return self


class EpisodesFile(StrictModel):
    schema_version: Literal["metrics-episodes-v1"]
    episodes: Seq[EpisodeDeclaration] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_ids(self) -> EpisodesFile:
        ids = [episode.episode_id for episode in self.episodes]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError(
                "duplicate_episode", "episode ids must be unique: {ids}", {"ids": ids}
            )
        return self


class TimingEvent(StrictModel):
    """One recorded human-session marker; ``timestamp_unix`` may be null."""

    episode_id: Identifier
    phase: TimingPhase
    marker: Literal["start", "end"]
    timestamp_unix: int | None = Field(default=None, ge=0, strict=True)
    source: Literal["recorded", "self_report"] = "recorded"


class AuthoredClaim(StrictModel):
    """A KPI claim asserted by an author; the gate recomputes it."""

    claim_id: Identifier
    metric: ClaimMetricName
    value: int = Field(ge=0, strict=True)


class ClaimsFile(StrictModel):
    schema_version: Literal["metrics-claims-v1"]
    claims: Seq[AuthoredClaim] = ()


__all__ = [
    "ACTIVE_HUMAN_TIME",
    "TIMING_PHASES",
    "AuthoredClaim",
    "ClaimMetricName",
    "ClaimsFile",
    "EpisodeDeclaration",
    "EpisodeKind",
    "EpisodesFile",
    "TimingEvent",
    "TimingPhase",
]
