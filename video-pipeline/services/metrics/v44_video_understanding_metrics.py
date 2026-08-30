"""T8 product-proof video-understanding metrics (pure aggregation).

Derives the ``VideoUnderstandingMetrics`` block of ``ProductProofReportV1``
from the committed fused ``MomentDeepReviewV1`` reviews' stage lineage —
never from runtime state, so the same numbers re-derive from the artifact
JSON alone. Stage instances are de-duplicated by
``(purpose, requested interval, input hash, pin hash)`` — the same call
under a different pin is a distinct call — while the shared
global-reduce stage and overlapping specialist stages are REPLICATED
across per-window reviews by design (T6 lineage order), so provider cost
counts each real provider call exactly once. The fused reviews' primary
``lineage.cost`` repeats the fusion stage's own cost and is therefore
never added on top of the stage sum. Coverage denominators come only
from the validated global-reduce interval; without that truth they stay
``None`` and are never inferred from (possibly incomplete) local windows.
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel
from services.media_intelligence.moment_review import (
    MomentDeepReviewV1,  # noqa: TC001 (pydantic field)
)

if TYPE_CHECKING:
    from services.media_intelligence.moment_review import ReviewStageLineage

#: Lead purposes ride the Gemini lead pin (local map + episode reduce).
LEAD_PURPOSES: Final[frozenset[str]] = frozenset({"local_map", "global_reduce"})

#: One real provider call: identical purpose+interval+input under the SAME
#: pin is one stage no matter how many per-window reviews replicate its
#: lineage record; a DIFFERENT pin hash is a distinct call (a re-pin must
#: not collapse spend).
_StageKey = tuple[str, int, int, str, str]


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _union_frames(spans: Sequence[tuple[int, int]]) -> int:
    """Unique frame count of half-open spans (overlap counts once).

    Local twin of ``budget.merge_window_spans`` arithmetic over plain int
    pairs — the budget seam takes DeepReviewWindow models, and importing it
    here would couple report derivation to window vocabulary it does not
    use.
    """

    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


class VideoUnderstandingMetrics(StrictModel):
    """T8 proof block: coverage, counts, retries, costs, stage pin hashes.

    Every nullable field stays ``None`` until truthfully measured (stage
    ``cost`` is ``None`` until a provider surfaces usage); counts are the
    de-duplicated stage-ledger truth. ``unresolved_count`` counts failed
    specialist targets carried into fusion as acknowledged unresolved
    uncertainty (hard failures block the run and never reach a report).
    """

    source_coverage: Annotated[float, Field(ge=0.0, le=1.0, strict=True)] | None = None
    local_window_count: Annotated[int, Field(ge=0, strict=True)]
    targeted_unique_coverage: Annotated[float, Field(ge=0.0, le=1.0, strict=True)] | None = (
        None
    )
    specialist_target_count: Annotated[int, Field(ge=0, strict=True)]
    specialist_success_count: Annotated[int, Field(ge=0, strict=True)]
    specialist_failure_count: Annotated[int, Field(ge=0, strict=True)]
    retry_count: Annotated[int, Field(ge=0, strict=True)]
    unresolved_count: Annotated[int, Field(ge=0, strict=True)]
    lead_cost: Annotated[float, Field(ge=0.0, strict=True)] | None = None
    specialist_cost: Annotated[float, Field(ge=0.0, strict=True)] | None = None
    fusion_cost: Annotated[float, Field(ge=0.0, strict=True)] | None = None
    stage_pin_sha256: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = ()

    @model_validator(mode="after")
    def require_specialist_counts_consistent(self) -> VideoUnderstandingMetrics:
        if (
            self.specialist_success_count + self.specialist_failure_count
            != self.specialist_target_count
        ):
            raise PydanticCustomError(
                "specialist_counts_mismatch",
                "specialist success + failure must equal the target count",
            )
        return self

    @property
    def total_cost(self) -> float | None:
        """Provider cost over the de-duplicated stage ledger.

        ``None`` while any role cost is unrecorded — a partial sum would
        under-report spend, so it is never silently summed.
        """

        if self.lead_cost is None or self.specialist_cost is None or self.fusion_cost is None:
            return None
        return self.lead_cost + self.specialist_cost + self.fusion_cost


def _stage_key(stage: ReviewStageLineage) -> _StageKey:
    return (
        stage.purpose,
        int(stage.requested_start_frame),
        int(stage.requested_end_frame),
        stage.input_sha256,
        stage.pin_sha256,
    )


def _role_cost(stages: Sequence[ReviewStageLineage]) -> float | None:
    total = 0.0
    for stage in stages:
        if stage.cost is None:
            return None
        total += float(stage.cost)
    return total


def _episode_interval(
    reduce_stages: Sequence[ReviewStageLineage],
) -> tuple[int, int] | None:
    """The validated full-episode interval from analyzed global-reduce truth.

    Only ``outcome="analyzed"`` reduce stages contribute, using their
    ANALYZED interval (validated equal to the request at orchestration) —
    a failed reduce request carries no analyzed span and must not
    establish a denominator, no matter how wide its requested bounds were.
    Every analyzed reduce stage must report the SAME interval (they are
    validations of one episode window); contradictory intervals or no
    analyzed reduce stage at all leave the denominator unknown — local
    windows are NEVER used to infer it (an incomplete map would shrink the
    denominator and manufacture coverage).
    """

    intervals = {
        (int(s.analyzed_start_frame), int(s.analyzed_end_frame))
        for s in reduce_stages
        if s.outcome == "analyzed"
        # Type narrowing only: the stage contract guarantees analyzed
        # stages carry both analyzed frames (outcome guard above).
        and s.analyzed_start_frame is not None
        and s.analyzed_end_frame is not None
    }
    if len(intervals) != 1:
        return None
    interval = next(iter(intervals))
    return interval if interval[1] > interval[0] else None


def compute_video_understanding_metrics(
    reviews: Sequence[MomentDeepReviewV1],
) -> VideoUnderstandingMetrics | None:
    """Derive the block from committed fused reviews; ``None`` when absent."""

    if not reviews:
        return None
    stages: dict[_StageKey, ReviewStageLineage] = {}
    for review in reviews:
        for stage in review.lineage.stage_lineage:
            stages[_stage_key(stage)] = stage
    ledger = tuple(stages.values())
    local = tuple(s for s in ledger if s.purpose == "local_map")
    lead_stages = tuple(s for s in ledger if s.purpose in LEAD_PURPOSES)
    specialist = tuple(s for s in ledger if s.purpose == "specialist")
    fusion = tuple(s for s in ledger if s.purpose == "fusion")

    interval = _episode_interval(
        tuple(s for s in ledger if s.purpose == "global_reduce")
    )
    source_coverage: float | None = None
    targeted: float | None = None
    if interval is not None:
        duration = interval[1] - interval[0]
        analyzed_local = tuple(
            (int(s.requested_start_frame), int(s.requested_end_frame))
            for s in local
            if s.outcome == "analyzed"
        )
        source_coverage = float(Fraction(_union_frames(analyzed_local), duration))
        requested_specialist = tuple(
            (int(s.requested_start_frame), int(s.requested_end_frame))
            for s in specialist
        )
        targeted = float(Fraction(_union_frames(requested_specialist), duration))
    failed_specialist = tuple(s for s in specialist if s.outcome == "failed")
    return VideoUnderstandingMetrics(
        source_coverage=source_coverage,
        local_window_count=len(local),
        targeted_unique_coverage=targeted,
        specialist_target_count=len(specialist),
        specialist_success_count=len(specialist) - len(failed_specialist),
        specialist_failure_count=len(failed_specialist),
        retry_count=sum(int(s.attempts) - 1 for s in ledger),
        unresolved_count=len(failed_specialist),
        lead_cost=_role_cost(lead_stages),
        specialist_cost=_role_cost(specialist),
        fusion_cost=_role_cost(fusion),
        stage_pin_sha256=tuple(sorted({s.pin_sha256 for s in ledger})),
    )


__all__ = [
    "LEAD_PURPOSES",
    "VideoUnderstandingMetrics",
    "compute_video_understanding_metrics",
]
