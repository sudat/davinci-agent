"""``AnalysisBudgetV1`` artifact and its enforcing builder.

Records where deep-review analysis attention was spent relative to the
Universal Pass, and why every window was triggered.  Coverage policy
(PRD 7.5 / SLO 232-238): deep review may cover at most
``policy_limits.deep_review_coverage_max`` (default 0.25) of the source
duration; exceeding the limit without at least one explicit
``expansion_reasons`` entry raises ``BudgetExpansionUnjustifiedError``.
Exceeding ``hard_coverage_cap`` is rejected even with reasons.

Deterministic: identical inputs produce byte-identical canonical output
(no clocks, randomness, or UUIDs; all ordering is sorted).  Wall clock is
an input recorded from execution — at plan time it is 0.0.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Frame, Identifier, RationalFrameRate, StrictModel

TriggerReason = Literal[
    "high_value",
    "uncertain",
    "visually_driven",
    "reaction_action_timing",
    "broll_near_block",
    "editorial_qc_flag",
    "human_request",
    "recall_audit_sample",
]

TRIGGER_REASONS: Final[frozenset[str]] = frozenset(
    (
        "high_value",
        "uncertain",
        "visually_driven",
        "reaction_action_timing",
        "broll_near_block",
        "editorial_qc_flag",
        "human_request",
        "recall_audit_sample",
    )
)


def _to_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class BudgetError(ValueError):
    """Base analysis-budget failure."""

    LABEL = "budget_error"


class BudgetExpansionUnjustifiedError(BudgetError):
    """Deep-review coverage exceeded the policy limit without a reason."""

    LABEL = "budget_expansion_unjustified"


# ---------------------------------------------------------------------------
# Artifact models
# ---------------------------------------------------------------------------


class DeepReviewWindow(StrictModel):
    """Half-open ``[start_frame, end_frame)`` deep-review window."""

    start_frame: Frame
    end_frame: Frame
    trigger_reason: TriggerReason
    trigger_source: Identifier

    @model_validator(mode="after")
    def require_forward(self) -> DeepReviewWindow:
        if self.end_frame < self.start_frame:
            raise PydanticCustomError(
                "span_inverted", "end_frame must be >= start_frame"
            )
        return self


class UniversalPassRecord(StrictModel):
    """Stage-1 Universal Pass record (wall clock filled by execution)."""

    wall_clock_seconds: float = Field(ge=0)
    shots_count: int = Field(ge=0)


class AnalysisCounters(StrictModel):
    """Frame/token/cost counters (plan-time tokens and cost are 0)."""

    frames: int = Field(ge=0)
    tokens: int = Field(default=0, ge=0)
    cost_estimate: float = Field(default=0.0, ge=0)


class PolicyLimits(StrictModel):
    """Coverage limits the scheduler must enforce."""

    deep_review_coverage_max: float = Field(default=0.25, ge=0, le=1)
    hard_coverage_cap: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def require_cap_at_least_limit(self) -> PolicyLimits:
        if self.hard_coverage_cap < self.deep_review_coverage_max:
            raise PydanticCustomError(
                "cap_below_limit", "hard_coverage_cap must be >= deep_review_coverage_max"
            )
        return self


class AnalysisBudgetV1(StrictModel):
    """Analysis budget artifact (``analysis-budget-v1``)."""

    schema_version: Literal["analysis-budget-v1"] = "analysis-budget-v1"
    source_duration_seconds: float = Field(ge=0)
    universal_pass: UniversalPassRecord
    deep_review_windows: Annotated[
        tuple[DeepReviewWindow, ...], BeforeValidator(_to_tuple)
    ] = Field(default_factory=tuple)
    reviewed_seconds: float = Field(ge=0)
    frame_or_token_counters: AnalysisCounters
    cache_hits: int = Field(default=0, ge=0)
    policy_limits: PolicyLimits
    expansion_reasons: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = Field(
        default_factory=tuple
    )


class BudgetRequest(StrictModel):
    """Typed inputs for :func:`build_analysis_budget`."""

    source_duration_frames: Frame
    frame_rate: RationalFrameRate
    universal_pass: UniversalPassRecord
    windows: Annotated[tuple[DeepReviewWindow, ...], BeforeValidator(_to_tuple)] = Field(
        default_factory=tuple
    )
    cache_hits: int = Field(default=0, ge=0)
    counters: AnalysisCounters | None = None
    policy_limits: PolicyLimits = Field(default_factory=PolicyLimits)
    expansion_reasons: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = Field(
        default_factory=tuple
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def merge_window_spans(
    windows: tuple[DeepReviewWindow, ...],
) -> tuple[tuple[int, int], ...]:
    """Union of half-open spans (sorted); overlapping spans count once."""

    merged: list[list[int]] = []
    for start, end in sorted(
        (int(window.start_frame), int(window.end_frame)) for window in windows
    ):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def _sorted_windows(
    windows: tuple[DeepReviewWindow, ...],
) -> tuple[DeepReviewWindow, ...]:
    return tuple(
        sorted(
            windows,
            key=lambda window: (
                window.start_frame,
                window.end_frame,
                window.trigger_reason,
                window.trigger_source,
            ),
        )
    )


def build_analysis_budget(request: BudgetRequest) -> AnalysisBudgetV1:
    """Build an ``AnalysisBudgetV1`` enforcing the coverage policy.

    Raises ``BudgetError`` for non-positive source duration or windows
    beyond the source, and ``BudgetExpansionUnjustifiedError`` when
    coverage exceeds ``deep_review_coverage_max`` without an explicit
    ``expansion_reasons`` entry.
    """

    duration = int(request.source_duration_frames)
    if duration <= 0:
        raise BudgetError("source duration must be > 0 frames for a budget")

    for window in request.windows:
        if int(window.end_frame) > duration:
            raise BudgetError(
                f"deep-review window exceeds source duration: {window.trigger_source} "
                f"end_frame={window.end_frame} > {duration}"
            )

    rate = request.frame_rate.as_fraction
    merged = merge_window_spans(request.windows)
    union_frames = sum(end - start for start, end in merged)
    reviewed_seconds = float(Fraction(union_frames) / rate)
    source_seconds = float(Fraction(duration) / rate)
    coverage = Fraction(union_frames, duration)

    limits = request.policy_limits
    if coverage > Fraction(str(limits.hard_coverage_cap)):
        raise BudgetError(
            f"deep-review coverage {float(coverage):.4f} exceeds the hard cap "
            f"{limits.hard_coverage_cap} even with expansion reasons"
        )
    if coverage > Fraction(str(limits.deep_review_coverage_max)) and not request.expansion_reasons:
        raise BudgetExpansionUnjustifiedError(
            f"deep-review coverage {float(coverage):.4f} exceeds limit "
            f"{limits.deep_review_coverage_max} without an expansion reason"
        )

    counters = request.counters or AnalysisCounters(frames=union_frames)
    return AnalysisBudgetV1(
        source_duration_seconds=source_seconds,
        universal_pass=request.universal_pass,
        deep_review_windows=_sorted_windows(request.windows),
        reviewed_seconds=reviewed_seconds,
        frame_or_token_counters=counters,
        cache_hits=request.cache_hits,
        policy_limits=limits,
        expansion_reasons=request.expansion_reasons,
    )
