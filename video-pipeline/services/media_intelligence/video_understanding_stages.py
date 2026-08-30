from __future__ import annotations

from typing import TYPE_CHECKING

from services.media_intelligence.budget import DeepReviewWindow
from services.media_intelligence.lead_map import generate_lead_map_windows
from services.media_intelligence.moment_review import (
    ReviewStageLineage,
    ReviewWindow,
)
from services.media_intelligence.video_review_wire import (
    GeminiClipReview,
    GlmClipObservation,
    VideoProviderError,
)
from services.media_intelligence.video_stage_wire import (
    GeminiEpisodeReduce,
    SpecialistRequestSpan,
)
from services.media_intelligence.video_understanding_assembly import (
    StageCall,
    analyzed_stage_lineage,
    failed_stage_lineage,
    overlapping,
    require_analyzed_span,
    require_stage_payload,
)
from services.media_intelligence.video_understanding_documents import (
    local_stage_document,
    reduce_stage_document,
    specialist_stage_document,
)
from services.media_intelligence.video_understanding_models import (
    VIDEO_UNDERSTANDING_TOOL,
    HardSpecialistTargetError,
    LocalStageRecord,
    SpecialistGap,
)
from services.media_intelligence.video_understanding_targets import (
    HARD_TRIGGER_REASONS,
    TargetSelection,
)

if TYPE_CHECKING:
    from services.media_intelligence.budget import DeepReviewWindow
    from services.media_intelligence.video_understanding_models import (
        VideoUnderstandingDeps,
        VideoUnderstandingRequest,
    )

#: Stage ledger entry keyed by the window the stage covered.
_Staged = tuple[ReviewWindow, ReviewStageLineage]


def run_local_map(
    request: VideoUnderstandingRequest, deps: VideoUnderstandingDeps
) -> tuple[LocalStageRecord, ...]:
    records: list[LocalStageRecord] = []
    for lead in generate_lead_map_windows(
        request.source_duration_frames,
        speech_boundaries=request.speech_boundaries,
        policy=request.lead_policy,
    ):
        window = ReviewWindow(start_frame=lead.start_frame, end_frame=lead.end_frame)
        pair = deps.clips.extract(window)
        transcript_ids = deps.transcripts.overlapping(window)
        audio = deps.audio.context(window)
        document = local_stage_document(
            request.episode_id, window, transcript_ids, audio.note
        )
        trace = deps.gemini.review_with_trace("local_map", pair.gemini, document)
        result = require_stage_payload("local_map", trace.payload, GeminiClipReview)
        analyzed = require_analyzed_span("local_map", result, window)
        records.append(
            LocalStageRecord(
                window=window,
                pair=pair,
                result=result,
                stage=analyzed_stage_lineage(
                    StageCall("local_map", deps.gemini.pin, pair.gemini, window, document),
                    analyzed,
                    result,
                    tool=VIDEO_UNDERSTANDING_TOOL,
                    attempts=trace.attempts,
                ),
                transcript_ids=transcript_ids,
                audio=audio,
            )
        )
    return tuple(records)


def run_reduce_stage(
    request: VideoUnderstandingRequest,
    deps: VideoUnderstandingDeps,
    local: tuple[LocalStageRecord, ...],
) -> tuple[ReviewStageLineage, GeminiEpisodeReduce]:
    """One episode reduce over the anchor clip + every local result.

    The lineage spans the validated full-source union (the anchor clip is
    merely the transport media); the typed result's ``specialist_requests``
    become selection candidates.
    """

    anchor = local[0]
    episode = ReviewWindow(
        start_frame=0, end_frame=request.source_duration_frames
    )
    document = reduce_stage_document(
        request.episode_id,
        tuple((r.window, r.result) for r in local),
        int(request.source_duration_frames),
    )
    trace = deps.gemini.review_with_trace("global_reduce", anchor.pair.gemini, document)
    result = require_stage_payload("global_reduce", trace.payload, GeminiEpisodeReduce)
    analyzed = require_analyzed_span("global_reduce", result, episode)
    stage = analyzed_stage_lineage(
        StageCall(
            "global_reduce", deps.gemini.pin, anchor.pair.gemini, episode, document
        ),
        analyzed,
        result,
        tool=VIDEO_UNDERSTANDING_TOOL,
        attempts=trace.attempts,
    )
    return stage, result


def _span_for_target(
    spans: tuple[SpecialistRequestSpan, ...], target: DeepReviewWindow
) -> SpecialistRequestSpan | None:
    bounds = (int(target.start_frame), int(target.end_frame))
    return next((s for s in spans if s.bounds == bounds), None)


def run_specialist_stages(
    request: VideoUnderstandingRequest,
    deps: VideoUnderstandingDeps,
    selection: TargetSelection,
    local: tuple[LocalStageRecord, ...],
) -> tuple[
    tuple[_Staged, ...],
    tuple[tuple[ReviewWindow, GlmClipObservation], ...],
    tuple[SpecialistGap, ...],
]:
    staged: list[_Staged] = []
    observations: list[tuple[ReviewWindow, GlmClipObservation]] = []
    gaps: list[SpecialistGap] = []
    summaries = tuple((r.window, r.result.summary) for r in local)
    for target in selection.selected:
        window = ReviewWindow(start_frame=target.start_frame, end_frame=target.end_frame)
        pair = deps.clips.extract(window)
        span = _span_for_target(selection.reduce_spans, target)
        document = specialist_stage_document(
            request.episode_id,
            window,
            target,
            span,
            [summary for _, summary in overlapping(summaries, window)],
        )
        call = StageCall("specialist", deps.specialist.pin, pair.glm, window, document)
        try:
            trace = deps.specialist.observe_with_trace(pair.glm, document)
        except VideoProviderError as error:
            staged.append(
                (
                    window,
                    failed_stage_lineage(
                        call, tool=VIDEO_UNDERSTANDING_TOOL, attempts=error.attempts
                    ),
                )
            )
            if target.trigger_reason in HARD_TRIGGER_REASONS:
                raise HardSpecialistTargetError(
                    f"hard specialist target [{target.start_frame}, {target.end_frame}) "
                    f"({target.trigger_source}) failed after {error.attempts} adapter "
                    f"attempt(s): {error.code} — blocking rather than omitting a "
                    "required target"
                ) from error
            gaps.append(
                SpecialistGap(
                    start_frame=target.start_frame,
                    end_frame=target.end_frame,
                    code=error.code,
                )
            )
            continue
        observation = require_stage_payload("specialist", trace.payload, GlmClipObservation)
        analyzed = require_analyzed_span("specialist", observation, window)
        staged.append(
            (
                window,
                analyzed_stage_lineage(
                    call,
                    analyzed,
                    observation,
                    tool=VIDEO_UNDERSTANDING_TOOL,
                    attempts=trace.attempts,
                ),
            )
        )
        observations.append((window, observation))
    return tuple(staged), tuple(observations), tuple(gaps)


__all__ = [
    "run_local_map",
    "run_reduce_stage",
    "run_specialist_stages",
]
