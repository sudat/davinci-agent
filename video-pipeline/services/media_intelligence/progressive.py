"""Three-stage progressive analysis router (PRD 7.5, implementation plan 6.5).

Stage 1 ``Universal Pass`` — all material is assumed scanned by the cheap
universal analyzers; the planner records the pass.  Stage 2 ``Editorial
Triage`` — every shot gets coarse deterministic scores.  Stage 3 ``Moment
Deep Review`` — trigger rules select windows, each with a trigger reason
and source ref.  No execution and no LLM here: task 17+ consumes the plan.

Contract: triage scores (and ``router_score``) are analysis-budget ROUTERS
only — they never encode keep/remove or any final edit intent.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Frame,
    Identifier,
    RationalFrameRate,
    StrictModel,
    to_tuple,
)
from services.media_intelligence.budget import (
    AnalysisBudgetV1,
    BudgetRequest,
    DeepReviewWindow,
    PolicyLimits,
    TriggerReason,
    UniversalPassRecord,
    build_analysis_budget,
)

if TYPE_CHECKING:
    from services.media_intelligence.models import MediaIntelligenceArtifact, Shot

_HUMAN_REQUEST_SOURCE: Final = "policy:human_request"
_SELECT_POTENTIAL_MAP: Final[dict[str, float]] = {"high": 0.9, "medium": 0.5, "low": 0.2}
_CONFIDENCE_UNCERTAINTY_MAP: Final[dict[str, float]] = {"high": 0.1, "medium": 0.5, "low": 0.9}
_BROLL_ROLES: Final[frozenset[str]] = frozenset({"broll", "b_roll"})
_REACTION_ROLES: Final[frozenset[str]] = frozenset(
    {"reaction", "action", "demonstration", "visual_comedy", "timing"}
)
_SCORE_DECIMALS: Final = 6


class ProgressivePlanError(ValueError):
    """Progressive planning failure."""

    LABEL = "progressive_plan_error"


class HumanReviewWindow(StrictModel):
    """Explicit human-requested review span (half-open frames)."""

    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> HumanReviewWindow:
        if self.end_frame < self.start_frame:
            raise PydanticCustomError("span_inverted", "end_frame must be >= start_frame")
        return self


class ProgressivePolicy(StrictModel):
    """Deterministic routing thresholds and explicit overrides."""

    frame_rate: RationalFrameRate = Field(
        default_factory=lambda: RationalFrameRate(num=30, den=1)
    )
    deep_review_coverage_max: float = Field(default=0.25, ge=0, le=1)
    high_value_threshold: float = Field(default=0.75, ge=0, le=1)
    uncertain_threshold: float = Field(default=0.6, ge=0, le=1)
    story_relevance_threshold: float = Field(default=0.5, ge=0, le=1)
    low_stratum_quantile: float = Field(default=0.25, gt=0, le=1)
    recall_audit_sample_count: int = Field(default=0, ge=0)
    human_review_windows: Annotated[
        tuple[HumanReviewWindow, ...], BeforeValidator(to_tuple)
    ] = Field(default_factory=tuple)
    expansion_reasons: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )


class TriageEntry(StrictModel):
    """Stage-2 coarse scores — budget routers, never keep/remove decisions."""

    shot_id: Identifier
    role: str
    story_relevance: float = Field(ge=0, le=1)
    select_potential: float = Field(ge=0, le=1)
    novelty: float = Field(ge=0, le=1)
    uncertainty: float = Field(ge=0, le=1)
    router_score: float = Field(ge=0, le=1)


class ProgressivePlan(StrictModel):
    """Deterministic routing plan (``progressive-plan-v1``)."""

    schema_version: Literal["progressive-plan-v1"] = "progressive-plan-v1"
    episode_id: Identifier
    triage: Annotated[tuple[TriageEntry, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )
    deep_review_windows: Annotated[
        tuple[DeepReviewWindow, ...], BeforeValidator(to_tuple)
    ] = Field(default_factory=tuple)
    budget: AnalysisBudgetV1


# ---------------------------------------------------------------------------
# Stage 2: deterministic coarse scoring over artifact fields
# ---------------------------------------------------------------------------


def _round(value: float) -> float:
    return round(max(0.0, min(1.0, value)), _SCORE_DECIMALS)


def _story_relevance(shot: Shot) -> float:
    return _round(
        0.1
        + (0.5 if shot.transcript_segments else 0.0)
        + (0.2 if shot.speaker_info is not None else 0.0)
        + (0.1 if shot.word_timings else 0.0)
    )


def _select_score(shot: Shot) -> float:
    return _round(_SELECT_POTENTIAL_MAP.get(shot.editorial.select_potential.lower(), 0.5))


def _novelty(shot: Shot) -> float:
    return _round(
        1.0
        - 0.2 * len(shot.similarity_refs or ())
        + (0.2 if shot.object_refs or shot.product_refs or shot.visible_texts else 0.0)
    )


def _uncertainty(shot: Shot) -> float:
    editorial = _CONFIDENCE_UNCERTAINTY_MAP.get(shot.confidence.editorial.lower(), 0.5)
    visual = _CONFIDENCE_UNCERTAINTY_MAP.get(shot.confidence.visual.lower(), 0.5)
    return _round((editorial + visual) / 2 + (0.1 if shot.visual.quality_flags else 0.0))


def _triage_shot(shot: Shot) -> TriageEntry:
    story = _story_relevance(shot)
    select = _select_score(shot)
    novelty = _novelty(shot)
    uncertainty = _uncertainty(shot)
    return TriageEntry(
        shot_id=shot.shot_id,
        role=shot.editorial.role,
        story_relevance=story,
        select_potential=select,
        novelty=novelty,
        uncertainty=uncertainty,
        router_score=_round(
            0.35 * select + 0.25 * story + 0.25 * novelty + 0.15 * uncertainty
        ),
    )


# ---------------------------------------------------------------------------
# Stage 3: trigger evaluation
# ---------------------------------------------------------------------------


def _shot_window(shot: Shot, reason: TriggerReason) -> DeepReviewWindow:
    span = shot.source_span
    return DeepReviewWindow(
        start_frame=span.start_frame,
        end_frame=span.end_frame,
        trigger_reason=reason,
        trigger_source=shot.shot_id,
    )


def _visually_driven(shot: Shot) -> bool:
    has_visual = bool(
        shot.face_reaction_cues or shot.visible_texts or shot.object_refs or shot.subject_action
    )
    return not shot.transcript_segments and has_visual


def _shot_triggers(
    shot: Shot,
    entry: TriageEntry,
    neighbours: tuple[TriageEntry, ...],
    policy: ProgressivePolicy,
) -> tuple[DeepReviewWindow, ...]:
    role = shot.editorial.role.lower()
    near_story_block = role in _BROLL_ROLES and any(
        neighbour.story_relevance >= policy.story_relevance_threshold
        for neighbour in neighbours
    )
    rules: tuple[tuple[bool, TriggerReason], ...] = (
        (entry.router_score >= policy.high_value_threshold, "high_value"),
        (entry.uncertainty >= policy.uncertain_threshold, "uncertain"),
        (_visually_driven(shot), "visually_driven"),
        (role in _REACTION_ROLES or bool(shot.face_reaction_cues), "reaction_action_timing"),
        (near_story_block, "broll_near_block"),
        (bool(shot.visual.quality_flags or shot.visual_quality_detail), "editorial_qc_flag"),
    )
    return tuple(_shot_window(shot, reason) for fired, reason in rules if fired)


def _recall_audit_windows(
    triage: tuple[TriageEntry, ...],
    shots_by_id: dict[str, Shot],
    policy: ProgressivePolicy,
) -> tuple[DeepReviewWindow, ...]:
    """Deterministic low-stratum sample: lowest ``router_score`` first."""

    if policy.recall_audit_sample_count <= 0 or not triage:
        return ()
    by_score = sorted(triage, key=lambda entry: (entry.router_score, entry.shot_id))
    cutoff = min(len(by_score) - 1, math.ceil(len(by_score) * policy.low_stratum_quantile) - 1)
    stratum = [
        entry for entry in by_score if entry.router_score <= by_score[cutoff].router_score
    ]
    return tuple(
        _shot_window(shots_by_id[str(entry.shot_id)], "recall_audit_sample")
        for entry in stratum[: policy.recall_audit_sample_count]
    )


def _evaluate_triggers(
    ordered: tuple[Shot, ...],
    triage: tuple[TriageEntry, ...],
    policy: ProgressivePolicy,
) -> tuple[DeepReviewWindow, ...]:
    shots_by_id = {str(shot.shot_id): shot for shot in ordered}
    windows: list[DeepReviewWindow] = []
    for index, (shot, entry) in enumerate(zip(ordered, triage, strict=True)):
        neighbours = tuple(
            triage[n] for n in (index - 1, index + 1) if 0 <= n < len(triage)
        )
        windows.extend(_shot_triggers(shot, entry, neighbours, policy))
    windows.extend(
        DeepReviewWindow(
            start_frame=window.start_frame,
            end_frame=window.end_frame,
            trigger_reason="human_request",
            trigger_source=_HUMAN_REQUEST_SOURCE,
        )
        for window in policy.human_review_windows
    )
    windows.extend(_recall_audit_windows(triage, shots_by_id, policy))
    return tuple(
        sorted(
            dict.fromkeys(windows),
            key=lambda w: (w.start_frame, w.end_frame, w.trigger_reason, w.trigger_source),
        )
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _source_duration_frames(artifact: MediaIntelligenceArtifact) -> int:
    candidates = [int(source.duration_frames or 0) for source in artifact.sources]
    candidates.extend(int(shot.source_span.end_frame) for shot in artifact.shots)
    return max(candidates, default=0)


def plan_progressive_analysis(
    artifact: MediaIntelligenceArtifact,
    *,
    policy: ProgressivePolicy,
) -> ProgressivePlan:
    """Route analysis attention deterministically over one artifact.

    Raises ``ProgressivePlanError`` for a zero-duration source and
    ``BudgetExpansionUnjustifiedError`` when coverage exceeds the limit
    without an expansion reason.
    """

    duration_frames = _source_duration_frames(artifact)
    if duration_frames <= 0:
        raise ProgressivePlanError(
            "cannot plan progressive analysis over a zero-duration source"
        )

    ordered = tuple(
        sorted(
            artifact.shots,
            key=lambda s: (s.source_span.start_frame, s.source_span.end_frame, s.shot_id),
        )
    )
    triage = tuple(_triage_shot(shot) for shot in ordered)
    windows = _evaluate_triggers(ordered, triage, policy)

    budget = build_analysis_budget(
        BudgetRequest(
            source_duration_frames=duration_frames,
            frame_rate=policy.frame_rate,
            universal_pass=UniversalPassRecord(wall_clock_seconds=0.0, shots_count=len(ordered)),
            windows=windows,
            policy_limits=PolicyLimits(deep_review_coverage_max=policy.deep_review_coverage_max),
            expansion_reasons=policy.expansion_reasons,
        )
    )
    return ProgressivePlan(
        episode_id=artifact.episode_id,
        triage=triage,
        deep_review_windows=windows,
        budget=budget,
    )
