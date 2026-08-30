"""T6 stage-lineage builders and the typed fused-review assembly.

``StageCall`` groups one remote stage call's typed identity INCLUDING the
honest coverage span (the episode-wide union for the reduce stage — its
anchor clip is merely transport); the builders turn validated provider
output (or a typed failure) into additive ``ReviewStageLineage`` records
whose ``attempts`` is the ACTUAL transport attempt count from the trace.

The fused mapping is pure code over the STRICT ``GeminiFusionReview``
judgment: sub-span, cut handles, rationale candidates, and every confidence
value come from the typed fusion payload — no constants, no geometry
placeholders. Before mapping, the payload is validated locally: the best
sub-span must lie inside the window, and every unresolved advisory gap
overlapping the window must be explicitly acknowledged or the run blocks
typed (never a silent commit).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from services.foundation_io import canonical_model_bytes
from services.media_intelligence.moment_review import (
    BestSubSpan,
    MomentAssessment,
    ReviewConfidence,
    ReviewStageLineage,
    ReviewWindow,
)
from services.media_intelligence.video_understanding_documents import (
    document_sha256,
    stage_input_sha256,
)
from services.media_intelligence.video_understanding_models import (
    StageRangeMismatchError,
    VideoUnderstandingError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.editorial_v2.editorial_pins import EditorialPinV2
    from services.media_intelligence.video_clip_evidence import VideoClipEvidence
    from services.media_intelligence.video_review_wire import (
        GeminiClipReview,
        GlmClipObservation,
    )
    from services.media_intelligence.video_stage_wire import (
        GeminiEpisodeReduce,
        GeminiFusionReview,
    )
    from services.media_intelligence.video_understanding_models import SpecialistGap

StagePurpose = Literal["local_map", "global_reduce", "specialist", "fusion"]


@dataclass(frozen=True, slots=True)
class StageCall:
    """The typed identity of one remote stage call (Smell-2 grouping).

    ``coverage`` is the honest requested/analyzed span: the window for
    local/specialist/fusion stages, the validated full-source union for the
    episode reduce (whose ``clip`` is only the transport anchor).
    """

    purpose: StagePurpose
    pin: EditorialPinV2
    clip: VideoClipEvidence
    coverage: ReviewWindow
    document: str


def overlapping[T](
    windows: Sequence[tuple[ReviewWindow, T]], target: ReviewWindow
) -> tuple[tuple[ReviewWindow, T], ...]:
    return tuple(
        (window, payload)
        for window, payload in windows
        if int(window.start_frame) < int(target.end_frame)
        and int(window.end_frame) > int(target.start_frame)
    )


def _pin_sha256(pin: EditorialPinV2) -> str:
    return hashlib.sha256(canonical_model_bytes(pin)).hexdigest()


def analyzed_stage_lineage(
    call: StageCall,
    analyzed: tuple[int, int],
    output_model: (
        GeminiClipReview | GeminiEpisodeReduce | GeminiFusionReview | GlmClipObservation
    ),
    *,
    tool: str,
    attempts: int,
) -> ReviewStageLineage:
    """Build the analyzed-stage record from validated provider output."""

    return ReviewStageLineage(
        purpose=call.purpose,
        provider=call.pin.api_surface,
        model_id=call.pin.model_id,
        pin_sha256=_pin_sha256(call.pin),
        tool=tool,
        requested_start_frame=call.coverage.start_frame,
        requested_end_frame=call.coverage.end_frame,
        analyzed_start_frame=analyzed[0],
        analyzed_end_frame=analyzed[1],
        input_sha256=stage_input_sha256(call.clip.sha256, document_sha256(call.document)),
        output_sha256=hashlib.sha256(canonical_model_bytes(output_model)).hexdigest(),
        attempts=attempts,
        cost=None,
        outcome="analyzed",
    )


def failed_stage_lineage(call: StageCall, *, tool: str, attempts: int) -> ReviewStageLineage:
    """Build the failed-stage record — no analyzed span, no output hash."""

    return ReviewStageLineage(
        purpose=call.purpose,
        provider=call.pin.api_surface,
        model_id=call.pin.model_id,
        pin_sha256=_pin_sha256(call.pin),
        tool=tool,
        requested_start_frame=call.coverage.start_frame,
        requested_end_frame=call.coverage.end_frame,
        input_sha256=stage_input_sha256(call.clip.sha256, document_sha256(call.document)),
        attempts=attempts,
        cost=None,
        outcome="failed",
    )


def validate_fusion_payload(
    fusion: GeminiFusionReview, window: ReviewWindow, gaps: Sequence[SpecialistGap]
) -> None:
    """Typed pre-``record_review`` validation of the fusion judgment.

    The best sub-span must lie inside the reviewed window, and every
    unresolved advisory gap overlapping the window must be explicitly
    acknowledged by the fusion output — an omission blocks typed rather
    than silently committing unacknowledged uncertainty.
    """

    start, end = int(window.start_frame), int(window.end_frame)
    sub = fusion.best_sub_span
    if not start <= int(sub.start_frame) <= int(sub.end_frame) <= end:
        raise VideoUnderstandingError(
            "fusion-payload-invalid",
            f"fusion best sub-span [{sub.start_frame}, {sub.end_frame}] lies outside "
            f"the reviewed window [{start}, {end})",
        )
    acknowledged = {
        (int(ack.start_frame), int(ack.end_frame))
        for ack in fusion.unresolved_acknowledgements
    }
    missing = [
        gap for gap in gaps
        if gap.overlaps(window) and (int(gap.start_frame), int(gap.end_frame)) not in acknowledged
    ]
    if missing:
        rendered = ", ".join(f"[{g.start_frame}, {g.end_frame})" for g in missing)
        raise VideoUnderstandingError(
            "fusion-unresolved-unacknowledged",
            f"fusion did not explicitly acknowledge unresolved specialist gap(s) "
            f"{rendered} overlapping [{start}, {end}) — refusing to commit "
            "unacknowledged uncertainty",
        )


def _gap_rationales(gaps: Sequence[SpecialistGap], window: ReviewWindow) -> tuple[str, ...]:
    return tuple(
        f"unresolved advisory specialist uncertainty over "
        f"[{gap.start_frame}, {gap.end_frame}) ({gap.code})"
        for gap in gaps
        if gap.overlaps(window)
    )


def fused_assessment(
    fusion: GeminiFusionReview, gaps: Sequence[SpecialistGap], window: ReviewWindow
) -> MomentAssessment:
    """Map the typed fusion judgment into assessment fields.

    Every semantic field is the fusion payload's own typed judgment; the
    deterministic append adds the explicit gap rationale for unresolved
    advisory failures (evidence text only — never a keep/remove decision).
    """

    return MomentAssessment(
        subject_action_evolution=fusion.subject_action_evolution,
        reaction_notes=fusion.reaction_notes,
        timing_notes=fusion.timing_notes,
        best_sub_span=BestSubSpan(
            start_frame=fusion.best_sub_span.start_frame,
            end_frame=fusion.best_sub_span.end_frame,
        ),
        keep_rationale_candidates=fusion.keep_rationale_candidates,
        remove_rationale_candidates=(
            *fusion.remove_rationale_candidates,
            *_gap_rationales(gaps, window),
        ),
        cut_in_handle=fusion.cut_in_handle,
        cut_out_handle=fusion.cut_out_handle,
    )


def fused_confidence(fusion: GeminiFusionReview) -> ReviewConfidence:
    """Every confidence value is the fusion judgment's own typed number."""

    values = fusion.confidence
    return ReviewConfidence(
        overall=values.overall,
        subject_action_evolution=values.subject_action_evolution,
        reaction_notes=values.reaction_notes,
        timing_notes=values.timing_notes,
        best_sub_span=values.best_sub_span,
    )


def require_analyzed_span(
    purpose: str,
    result: GeminiClipReview | GeminiEpisodeReduce | GeminiFusionReview | GlmClipObservation,
    window: ReviewWindow,
) -> tuple[int, int]:
    """A provider's claimed analyzed span must equal the requested window."""

    analyzed = (int(result.analyzed_start_frame), int(result.analyzed_end_frame))
    requested = (int(window.start_frame), int(window.end_frame))
    if analyzed != requested:
        raise StageRangeMismatchError(
            f"{purpose} claims analyzed [{analyzed[0]}, {analyzed[1]}) but the clip "
            f"requested [{requested[0]}, {requested[1]}) — provider spans are "
            "observations, never authority"
        )
    return analyzed


def require_stage_payload[ModelT](
    purpose: str, payload: object, expected: type[ModelT]
) -> ModelT:
    """Narrow the provider's stage-union payload to the expected DTO.

    The wire registry selects by purpose inside the provider; this is the
    orchestration's own boundary check that the promised type arrived.
    """

    if not isinstance(payload, expected):
        raise StageRangeMismatchError(
            f"{purpose} stage returned {type(payload).__name__}, expected "
            f"{expected.__name__} — the purpose-routed wire contract was violated"
        )
    return payload


__all__ = [
    "StageCall",
    "StagePurpose",
    "analyzed_stage_lineage",
    "failed_stage_lineage",
    "fused_assessment",
    "fused_confidence",
    "overlapping",
    "require_analyzed_span",
    "require_stage_payload",
    "validate_fusion_payload",
]
