"""T6 fusion phase: one typed fusion call, then the single record commit.

``FusionContext`` groups the run-scoped facts every fused review consumes
(the reduce stage record rides along so reduce provenance reaches the
committed artifact); ``build_fused_review`` runs the fusion call over the
window's transport clip + typed document, validates the analyzed span, the
best sub-span, and every unresolved-gap acknowledgement, then commits
through the EXISTING ``record_review`` path with the stage ledger ordered
local → global_reduce → overlapping specialists → fusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from services.media_intelligence.moment_review import (
    ReviewEvidence,
    ReviewLineage,
    ReviewRecordRequest,
    ReviewStageLineage,
    ReviewWindow,
    record_review,
)
from services.media_intelligence.video_stage_wire import GeminiFusionReview
from services.media_intelligence.video_understanding_assembly import (
    StageCall,
    analyzed_stage_lineage,
    fused_assessment,
    fused_confidence,
    overlapping,
    require_analyzed_span,
    require_stage_payload,
    validate_fusion_payload,
)
from services.media_intelligence.video_understanding_documents import (
    FusionInputs,
    fusion_stage_document,
)
from services.media_intelligence.video_understanding_models import (
    VIDEO_UNDERSTANDING_TOOL,
)

if TYPE_CHECKING:
    from services.media_intelligence.budget import DeepReviewWindow
    from services.media_intelligence.moment_review import MomentDeepReviewV1
    from services.media_intelligence.video_review_wire import GlmClipObservation
    from services.media_intelligence.video_stage_wire import GeminiEpisodeReduce
    from services.media_intelligence.video_understanding_models import (
        LocalStageRecord,
        SpecialistGap,
        VideoUnderstandingDeps,
        VideoUnderstandingRequest,
    )

#: Specialist ledger entry keyed by the window the stage covered.
_Staged = tuple[ReviewWindow, ReviewStageLineage]


@dataclass(frozen=True, slots=True)
class FusionContext:
    """Run-scoped facts every fused review consumes (Smell-2 grouping)."""

    reduce_stage: ReviewStageLineage
    reduce_result: GeminiEpisodeReduce
    staged: tuple[_Staged, ...]
    observations: tuple[tuple[ReviewWindow, GlmClipObservation], ...]
    gaps: tuple[SpecialistGap, ...]
    deferred: tuple[DeepReviewWindow, ...]


def build_fused_review(
    request: VideoUnderstandingRequest,
    deps: VideoUnderstandingDeps,
    record: LocalStageRecord,
    context: FusionContext,
) -> MomentDeepReviewV1:
    """Run one fusion call over the window and commit it via ``record_review``."""

    document = fusion_stage_document(
        FusionInputs(
            episode_id=request.episode_id,
            window=record.window,
            local=record.result,
            reduce_result=context.reduce_result,
            specialists=overlapping(context.observations, record.window),
            gaps=context.gaps,
            deferred=context.deferred,
            transcript_ids=record.transcript_ids,
            audio_note=record.audio.note,
        )
    )
    trace = deps.gemini.review_with_trace("fusion", record.pair.gemini, document)
    fusion = require_stage_payload("fusion", trace.payload, GeminiFusionReview)
    require_analyzed_span("fusion", fusion, record.window)
    validate_fusion_payload(fusion, record.window, context.gaps)
    fusion_stage = analyzed_stage_lineage(
        StageCall("fusion", deps.gemini.pin, record.pair.gemini, record.window, document),
        (int(fusion.analyzed_start_frame), int(fusion.analyzed_end_frame)),
        fusion,
        tool=VIDEO_UNDERSTANDING_TOOL,
        attempts=trace.attempts,
    )
    attached = tuple(stage for _, stage in overlapping(context.staged, record.window))
    return record_review(
        ReviewRecordRequest(
            episode_id=request.episode_id,
            source_duration_frames=request.source_duration_frames,
            known_shot_ids=request.known_shot_ids,
            window=record.window,
            evidence=ReviewEvidence(
                frame_bundle=(),
                transcript_refs=record.transcript_ids,
                audio_context=record.audio,
            ),
            assessment=fused_assessment(fusion, context.gaps, record.window),
            confidence=fused_confidence(fusion),
            lineage=ReviewLineage(
                provider=deps.gemini.pin.api_surface,
                provider_version=deps.gemini.pin.model_id,
                tool=VIDEO_UNDERSTANDING_TOOL,
                cost=None,
                stage_lineage=(record.stage, context.reduce_stage, *attached, fusion_stage),
            ),
        )
    )


__all__ = [
    "FusionContext",
    "build_fused_review",
]
