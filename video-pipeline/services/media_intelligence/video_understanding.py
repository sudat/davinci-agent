"""T6 video-understanding orchestration: local map → global reduce →
targeted GLM specialists → Gemini fusion (plan task 6 / PRD 6.7.1 roles).

Evidence-only seam: it consumes the T4 provider adapters and the T5
budget/window contracts, validates every requested/analyzed span locally,
enforces exact full-source local coverage through the lead-map budget, and
commits exactly ONE fused ``MomentDeepReviewV1`` per local window through the
EXISTING ``record_review`` path. It never chooses keep/remove/order/edit
intent and never writes Job State, Selection Plan, Edit Plan, or Resolve —
T7 places this evidence before the Director.

Failure semantics: a Gemini local/reduce/fusion failure blocks the run (the
adapters' own single parser-retry is the bounded retry — none is added
here); a HARD specialist target (a human request) blocks typed; an advisory
failure continues only with the unresolved uncertainty recorded for fusion
(lowered confidence plus an explicit remove-rationale candidate); a provider
claiming a different span than requested is never labeled analyzed and
blocks typed. The global reduce anchors on the FIRST local window's clip
(its real input — every local result — rides the untrusted document).

This module re-exports the seam's public value types so consumers import
from ONE place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.media_intelligence.budget import (
    AnalysisBudgetV1,
    BudgetRequest,
    DeepReviewWindow,
    UniversalPassRecord,
    build_analysis_budget,
)
from services.media_intelligence.lead_map import LEAD_MAP_TRIGGER_SOURCE
from services.media_intelligence.video_understanding_fusion import (
    FusionContext,
    build_fused_review,
)
from services.media_intelligence.video_understanding_models import (
    VIDEO_UNDERSTANDING_TOOL,
    ClipSource,
    GeminiStageCaller,
    HardSpecialistTargetError,
    SpecialistCaller,
    SpecialistGap,
    StageRangeMismatchError,
    VideoUnderstandingDeps,
    VideoUnderstandingError,
    VideoUnderstandingRequest,
    VideoUnderstandingResult,
)
from services.media_intelligence.video_understanding_stages import (
    run_local_map,
    run_reduce_stage,
    run_specialist_stages,
)
from services.media_intelligence.video_understanding_targets import (
    select_specialist_targets,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.contracts.primitives import RationalFrameRate

__all__ = [
    "VIDEO_UNDERSTANDING_TOOL",
    "ClipSource",
    "GeminiStageCaller",
    "HardSpecialistTargetError",
    "SpecialistCaller",
    "SpecialistGap",
    "StageRangeMismatchError",
    "VideoUnderstandingDeps",
    "VideoUnderstandingError",
    "VideoUnderstandingRequest",
    "VideoUnderstandingResult",
    "run_video_understanding",
    "validate_local_coverage",
]


def validate_local_coverage(
    source_duration_frames: int,
    analyzed_spans: Sequence[tuple[int, int]],
    frame_rate: RationalFrameRate,
) -> AnalysisBudgetV1:
    """Enforce the exact analyzed full-source union via the T5 lead-map budget."""

    windows = tuple(
        DeepReviewWindow(
            start_frame=start,
            end_frame=end,
            trigger_reason="lead_map_window",
            trigger_source=LEAD_MAP_TRIGGER_SOURCE,
        )
        for start, end in analyzed_spans
    )
    return build_analysis_budget(
        BudgetRequest(
            source_duration_frames=source_duration_frames,
            frame_rate=frame_rate,
            universal_pass=UniversalPassRecord(wall_clock_seconds=0.0, shots_count=0),
            windows=windows,
            budget_kind="lead_map",
        )
    )


def run_video_understanding(
    request: VideoUnderstandingRequest, deps: VideoUnderstandingDeps
) -> VideoUnderstandingResult:
    """Run the full map → reduce → targeted specialist → fusion flow."""

    if int(request.source_duration_frames) <= 0:
        raise VideoUnderstandingError(
            "invalid-source", "source_duration_frames must be > 0 for video understanding"
        )
    local = run_local_map(request, deps)
    lead_budget = validate_local_coverage(
        request.source_duration_frames,
        tuple((r.result.analyzed_start_frame, r.result.analyzed_end_frame) for r in local),
        request.frame_rate,
    )
    reduce_stage, reduce_result = run_reduce_stage(request, deps, local)
    selection = select_specialist_targets(
        request.source_duration_frames,
        request.progressive_windows,
        reduce_result.specialist_requests,
        frame_rate=request.frame_rate,
    )
    staged, observations, gaps = run_specialist_stages(request, deps, selection, local)
    context = FusionContext(
        reduce_stage=reduce_stage,
        reduce_result=reduce_result,
        staged=staged,
        observations=observations,
        gaps=gaps,
        deferred=selection.deferred,
    )
    reviews = tuple(build_fused_review(request, deps, record, context) for record in local)
    return VideoUnderstandingResult(
        reviews=reviews,
        lead_map_budget=lead_budget,
        targeted_budget=selection.targeted_budget,
        reduce_stage=reduce_stage,
        specialist_stages=tuple(stage for _, stage in staged),
        deferred=selection.deferred,
    )
