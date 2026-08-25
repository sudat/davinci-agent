"""The REAL V44-0 arm pipeline: chain stages → DirectorV2 → commit → reviews.

Task-14 execution enabler. Wraps (never forks) the existing chain machinery
(chain stages via ``v44_arm_stages``), then drives the production editorial path: the
speech-segment MI artifact → v2 index → ``DirectorV2().run_three_pass`` over
an INJECTED ``llm_call`` (codex-exec in production, fakes in tests) → the
existing ``validate_proposal``/``commit_selection`` authority → arm-B
targeted real Moment Deep Reviews via T4's ``execute_real_review`` with an
injected assessment provider.

Honesty contract: kept spans come ONLY from the committed selection
proposal; a director refusal or validation failure IS the result (typed,
never a fallback). Every model-facing artifact lands under the arm
workspace for spot-checks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli._v44_arm_proper_nouns import substituted_arm_transcript
from services.cli.v44_arm_evidence import build_speech_mi_artifact
from services.cli.v44_arm_stages import (
    ArmPipelineData,
    ArmPipelineError,
    run_arm_stages,
)
from services.editorial_v2.director_v2 import DirectorV2
from services.editorial_v2.evidence_v2 import assemble_evidence_v2
from services.editorial_v2.proposal_validate import (
    MomentSelectionStore,
    commit_selection,
    initialize_moment_store,
    validate_proposal,
)
from services.foundation_io import atomic_write, canonical_model_bytes
from services.media_intelligence.moment_review import (
    MomentDeepReviewV1,
    NeighboringContext,
    ReviewExecutionContext,
    ReviewWindow,
)
from services.media_intelligence.moment_review_real import (
    AssessmentPinSummary,
    RealAudioContext,
    RealFrameExtractor,
    RealReviewProviders,
    RealReviewSetup,
    RealTranscriptLookup,
    execute_real_review,
    require_real_lineage,
)
from services.media_query.index_v2 import build_index
from services.media_query.query_v2 import MediaQueryApiV2

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.editorial_v2.director_v2 import LlmCallV2, ThreePassResult
    from services.editorial_v2.episode_brief import EpisodeBriefV1
    from services.editorial_v2.moment_models import MomentCandidateV2
    from services.editorial_v2.proposal_validate import CommitReceipt
    from services.media_intelligence.moment_review_real import AssessmentProvider

#: Arm-B escalation policy: a kept candidate whose deep-review confidence
#: falls below this is DEMOTED from the kept spans and ESCALATED (recorded,
#: never silently kept nor silently dropped).
ESCALATE_BELOW: Final = 0.5
#: Reviews target the LOWEST-CONFIDENCE keeps (the borderline windows).
MAX_REVIEWS: Final = 3
_CANDIDATE_PREFIX: Final = "cand-"






@dataclass(frozen=True, slots=True)
class ArmPipelineInputs:
    """One arm run's wiring; everything model- or media-bound is injected."""

    episode_root: Path
    workspace: Path
    episode_id: str
    brief: EpisodeBriefV1
    llm_call: LlmCallV2
    assessment: AssessmentProvider | None = None
    assessment_pin: tuple[str, str] = ("codex-exec", "gpt-5.6-sol")
    review_providers: RealReviewProviders | None = None
    analysis: ArmPipelineData | None = None


@dataclass(frozen=True, slots=True)
class ArmPipelineResult:
    """The committed selection facts the report derives from."""

    kept_spans_mezz: tuple[tuple[int, int], ...]
    kept_candidate_ids: tuple[str, ...]
    escalated_candidate_ids: tuple[str, ...]
    escalated_spans_mezz: tuple[tuple[int, int], ...]
    reviews: tuple[MomentDeepReviewV1, ...] = ()
    commit_version: int = 0
    proposal_id: str = ""
    candidate_count: int = 0
    wall_seconds: float = 0.0
    hypothesis_segments_ms: tuple[tuple[int, int, str], ...] = ()
    notes: tuple[str, ...] = ()




def _persist_drafts(
    workspace: Path, result: ThreePassResult, artifact_bytes: bytes
) -> None:
    atomic_write(workspace / "story-plan.json", canonical_model_bytes(result.story_plan))
    atomic_write(
        workspace / "moment-selection.json",
        canonical_model_bytes(result.moment_selection),
    )
    atomic_write(
        workspace / "creative-edit.json", canonical_model_bytes(result.creative_edit)
    )
    atomic_write(workspace / "media-intelligence.json", artifact_bytes)


def _shot_id_of(candidate_id: str) -> str:
    return candidate_id.removeprefix(_CANDIDATE_PREFIX)


def _run_reviews(
    inputs: ArmPipelineInputs,
    data: ArmPipelineData,
    api: MediaQueryApiV2,
    kept_candidates: Sequence[MomentCandidateV2],
) -> tuple[tuple[MomentDeepReviewV1, ...], tuple[str, ...]]:
    """Targeted real deep reviews on the lowest-confidence keeps."""

    by_time = sorted(
        kept_candidates, key=lambda c: (c.source_span.start_frame, c.candidate_id)
    )
    targets = sorted(
        kept_candidates,
        key=lambda c: (c.confidence, c.source_span.start_frame),
    )[:MAX_REVIEWS]
    if inputs.assessment is None or not targets:
        return (), ()
    providers = inputs.review_providers
    if providers is None:
        if data.mezzanine is None or data.mezzanine_sha256 is None:
            raise ArmPipelineError(
                "review_media_missing", "arm B review providers need the mezzanine"
            )
        providers = RealReviewProviders(
            frames=RealFrameExtractor(
                media_path=data.mezzanine,
                media_sha256=data.mezzanine_sha256,
                analysis_dir=inputs.workspace / "moment-review",
            ),
            transcripts=RealTranscriptLookup(api=api, source_id=data.source_id),
            audio=RealAudioContext(api=api),
        )
    provider, model_id = inputs.assessment_pin
    setup = RealReviewSetup(
        providers=providers,
        assessment=inputs.assessment,
        pin=AssessmentPinSummary(provider=provider, model_id=model_id),
        brief_context=inputs.brief.viewer_promise,
    )
    known = frozenset(_shot_id_of(str(c.candidate_id)) for c in kept_candidates)
    position_of = {str(c.candidate_id): index for index, c in enumerate(by_time)}
    reviews: list[MomentDeepReviewV1] = []
    escalated: list[str] = []
    review_dir = inputs.workspace / "moment-review"
    for candidate in targets:
        position = position_of[str(candidate.candidate_id)]
        window = ReviewWindow(
            start_frame=candidate.source_span.start_frame,
            end_frame=min(candidate.source_span.end_frame, data.total_frames),
        )
        neighbors = NeighboringContext(
            prev_shot_id=_shot_id_of(str(by_time[position - 1].candidate_id))
            if position > 0
            else None,
            next_shot_id=_shot_id_of(str(by_time[position + 1].candidate_id))
            if position + 1 < len(by_time)
            else None,
        )
        review = execute_real_review(
            window,
            setup,
            context=ReviewExecutionContext(
                episode_id=data.episode_id,  # type: ignore[arg-type]
                source_duration_frames=data.total_frames,  # type: ignore[arg-type]
                known_shot_ids=known,
                neighbors=neighbors,
            ),
        )
        reviews.append(review)
        atomic_write(
            review_dir / f"{review.review_id}.json", canonical_model_bytes(review)
        )
        if float(review.confidence.overall) < ESCALATE_BELOW:
            escalated.append(str(candidate.candidate_id))
    require_real_lineage(tuple(reviews))
    return tuple(reviews), tuple(escalated)


def run_arm_pipeline(inputs: ArmPipelineInputs) -> ArmPipelineResult:
    """Drive one real arm end-to-end; kept spans come from the COMMIT only."""

    started = time.monotonic()
    data = (
        inputs.analysis
        if inputs.analysis is not None
        else run_arm_stages(inputs.episode_root, inputs.workspace, inputs.episode_id)
    )
    speech, hypothesis_ms, noun_edits = substituted_arm_transcript(
        data.speech, data.transcript_segments_ms, inputs.episode_root
    )
    artifact = build_speech_mi_artifact(
        data.episode_id, data.source_id, data.total_frames, speech
    )
    index_path = inputs.workspace / "media-intelligence.duckdb"
    build_index(artifact, index_path)
    notes = [f"speech_shots={len(data.speech)}", f"proper_noun_substitutions={noun_edits}"]
    with MediaQueryApiV2.open(index_path) as api:
        three = DirectorV2().run_three_pass(
            inputs.brief, api, taste_profile=None, llm_call=inputs.llm_call
        )
        proposal = three.moment_selection.proposal
        bundle = assemble_evidence_v2(api, proposal.candidates, source_id=data.source_id)
        validation = validate_proposal(proposal, api, bundle)
        store = MomentSelectionStore(plan_dir=inputs.workspace / "moment-selection")
        initialize_moment_store(store, episode_id=data.episode_id)
        receipt: CommitReceipt = commit_selection(proposal, validation, store=store)
        kept = [c for c in proposal.candidates if c.intent == "keep"]
        reviews: tuple[MomentDeepReviewV1, ...] = ()
        escalated: tuple[str, ...] = ()
        if inputs.assessment is not None:
            reviews, escalated = _run_reviews(inputs, data, api, kept)
    _persist_drafts(inputs.workspace, three, canonical_model_bytes(artifact))
    escalated_set = set(escalated)
    kept_after = [c for c in kept if str(c.candidate_id) not in escalated_set]
    kept_spans = tuple(
        (int(c.source_span.start_frame), int(c.source_span.end_frame)) for c in kept_after
    )
    escalated_spans = tuple(
        (int(c.source_span.start_frame), int(c.source_span.end_frame))
        for c in kept
        if str(c.candidate_id) in escalated_set
    )
    if escalated:
        notes.append(
            f"arm_b_escalation: {len(escalated)} keep(s) demoted+escalated at review "
            f"confidence < {ESCALATE_BELOW} ({', '.join(escalated)})"
        )
    notes.append(f"commit_version=v{receipt.version} proposal={proposal.proposal_id}")
    return ArmPipelineResult(
        kept_spans_mezz=kept_spans,
        kept_candidate_ids=tuple(str(c.candidate_id) for c in kept_after),
        escalated_candidate_ids=escalated,
        escalated_spans_mezz=escalated_spans,
        reviews=reviews,
        commit_version=receipt.version,
        proposal_id=str(proposal.proposal_id),
        candidate_count=len(proposal.candidates),
        wall_seconds=time.monotonic() - started,
        hypothesis_segments_ms=hypothesis_ms,
        notes=tuple(notes),
    )


__all__ = [
    "ESCALATE_BELOW",
    "MAX_REVIEWS",
    "ArmPipelineData",
    "ArmPipelineError",
    "ArmPipelineInputs",
    "ArmPipelineResult",
    "run_arm_pipeline",
    "run_arm_stages",
]
