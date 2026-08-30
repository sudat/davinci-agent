"""The REAL V44-0 arm pipeline: chain stages → video understanding →
DirectorV2 → the single commit → fused-evidence escalation.

Task-14 execution enabler, reordered for T7: wraps (never forks) the existing
chain machinery (``v44_arm_stages``), then drives the production editorial
path in the corrected order — speech-segment MI artifact → v2 index →
full-source video understanding (T6) → the SAME index rebuilt with the fused
``MomentDeepReviewV1`` reviews → ``DirectorV2().run_three_pass`` over an
INJECTED ``llm_call`` (codex-exec in production, fakes in tests) → the
existing ``validate_proposal``/``commit_selection`` authority (exactly one
commit) → the existing explicit escalation semantics over keeps whose fused
review confidence falls below the threshold.

Honesty contract: kept spans come ONLY from the committed selection
proposal; a video-understanding coverage/fusion failure, a synthetic fused
lineage, a director refusal, or a validation failure IS the result (typed,
never a fallback, and never a commit). Every model-facing artifact lands
under the arm workspace for spot-checks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli._v44_arm_integrity import (
    expected_candidate_ids,
    require_candidate_integrity,
    require_proposal_completeness,
)
from services.cli._v44_arm_proper_nouns import substituted_arm_transcript
from services.cli.v44_arm_evidence import build_speech_mi_artifact
from services.cli.v44_arm_stages import (
    ArmPipelineData,
    ArmPipelineError,
    run_arm_stages,
)
from services.editorial_v2.director_v2 import DirectorV2
from services.editorial_v2.evidence_v2 import EvidenceBundleV2, assemble_evidence_v2
from services.editorial_v2.proposal_validate import (
    MomentSelectionStore,
    commit_selection,
    initialize_moment_store,
    validate_proposal,
)
from services.foundation_io import atomic_write, canonical_model_bytes
from services.media_intelligence.moment_review_real import require_real_lineage
from services.media_intelligence.video_understanding import (
    VideoUnderstandingDeps,
    VideoUnderstandingRequest,
    run_video_understanding,
)
from services.media_query.index_v2 import build_index
from services.media_query.query_v2 import MediaQueryApiV2

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from services.editorial_v2.director_v2 import LlmCallV2, ThreePassResult
    from services.editorial_v2.episode_brief import EpisodeBriefV1
    from services.editorial_v2.moment_models import MomentCandidateV2
    from services.editorial_v2.proposal_validate import CommitReceipt
    from services.media_intelligence.moment_review import MomentDeepReviewV1

#: Arm-B escalation policy: a kept candidate whose OVERLAPPING fused review
#: confidence falls below this is DEMOTED from the kept spans and ESCALATED
#: (recorded, never silently kept nor silently dropped). T7 moves the
#: evidence BEFORE the Director; the policy application stays explicit.
ESCALATE_BELOW: Final = 0.5

#: Builds the video-understanding wiring once the chain stages produced the
#: mezzanine and the speech index exists (tests inject a constant factory).
type VideoUnderstandingFactory = Callable[
    [ArmPipelineData, MediaQueryApiV2], VideoUnderstandingDeps
]


@dataclass(frozen=True, slots=True)
class ArmPipelineInputs:
    """One arm run's wiring; everything model- or media-bound is injected.

    ``video_understanding=None`` is Arm A (baseline, no fused evidence); a
    factory turns the run into the corrected Arm B — fused evidence is then
    REQUIRED for every Director keep.
    """

    episode_root: Path
    workspace: Path
    episode_id: str
    brief: EpisodeBriefV1
    llm_call: LlmCallV2
    video_understanding: VideoUnderstandingFactory | None = None
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


def _fused_reviews(
    inputs: ArmPipelineInputs, data: ArmPipelineData, deps: VideoUnderstandingDeps
) -> tuple[MomentDeepReviewV1, ...]:
    """Full-source map → reduce → specialist → fusion; synthetic lineages
    and coverage failures refuse here, BEFORE any Director proposal."""

    boundaries = tuple(
        sorted(
            {
                frame
                for segment in data.speech
                for frame in (int(segment.start_frame), int(segment.end_frame))
            }
        )
    )
    request = VideoUnderstandingRequest(
        episode_id=data.episode_id,
        source_duration_frames=data.total_frames,
        known_shot_ids=frozenset(segment.segment_id for segment in data.speech),
        speech_boundaries=boundaries,
    )
    reviews = run_video_understanding(request, deps).reviews
    require_real_lineage(reviews)
    review_dir = inputs.workspace / "moment-review"
    for review in reviews:
        atomic_write(review_dir / f"{review.review_id}.json", canonical_model_bytes(review))
    return reviews


def _unconfirmed_keeps(
    bundle: EvidenceBundleV2, kept: Sequence[MomentCandidateV2]
) -> tuple[str, ...]:
    """Keeps whose overlapping fused review confidence is below the
    threshold; every keep has a bundle entry (validation proved it)."""

    by_id = {entry.candidate_id: entry for entry in bundle.entries}
    escalated: list[str] = []
    for candidate in kept:
        confidences = tuple(
            citation.overall_confidence
            for citation in by_id[candidate.candidate_id].moment_reviews
        )
        if confidences and min(confidences) < ESCALATE_BELOW:
            escalated.append(str(candidate.candidate_id))
    return tuple(escalated)


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
    # Task-2 paid-call boundary: the index must deliver EXACTLY the expected
    # speech candidates over [0, total_frames) before any Gemini/GLM/codex
    # call and before any Director proposal — equal counts never suffice.
    with MediaQueryApiV2.open(index_path) as integrity_api:
        require_candidate_integrity(integrity_api, data)
    expected_candidates = expected_candidate_ids(data)
    notes = [f"speech_shots={len(data.speech)}", f"proper_noun_substitutions={noun_edits}"]
    fused: tuple[MomentDeepReviewV1, ...] = ()
    if inputs.video_understanding is not None:
        # The factory's RealTranscriptLookup/RealAudioContext hold THIS api;
        # the fused reviews must run while the connection is still open
        # (measured 2026-08-30: closing here killed the real Arm B run).
        with MediaQueryApiV2.open(index_path) as speech_api:
            deps = inputs.video_understanding(data, speech_api)
            fused = _fused_reviews(inputs, data, deps)
        build_index(artifact, index_path, reviews=fused)
        notes.append(f"fused_reviews={len(fused)}")
    with MediaQueryApiV2.open(index_path) as api:
        three = DirectorV2().run_three_pass(
            inputs.brief,
            api,
            taste_profile=None,
            llm_call=inputs.llm_call,
            source_id=data.source_id,
            require_deep_review_keeps=inputs.video_understanding is not None,
            source_total_frames=data.total_frames,
        )
        proposal = three.moment_selection.proposal
        # Task-2 commit boundary: every discovered candidate must be proposed
        # — a missing proposal is a refusal, never an implicit drop.
        require_proposal_completeness(proposal, expected_candidates)
        bundle = assemble_evidence_v2(api, proposal.candidates, source_id=data.source_id)
        validation = validate_proposal(proposal, api, bundle)
        store = MomentSelectionStore(plan_dir=inputs.workspace / "moment-selection")
        initialize_moment_store(store, episode_id=data.episode_id)
        receipt: CommitReceipt = commit_selection(proposal, validation, store=store)
        kept = [c for c in proposal.candidates if c.intent == "keep"]
        escalated = _unconfirmed_keeps(bundle, kept)
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
            f"arm_b_escalation: {len(escalated)} keep(s) demoted+escalated at fused "
            f"review confidence < {ESCALATE_BELOW} ({', '.join(escalated)})"
        )
    notes.append(f"commit_version=v{receipt.version} proposal={proposal.proposal_id}")
    return ArmPipelineResult(
        kept_spans_mezz=kept_spans,
        kept_candidate_ids=tuple(str(c.candidate_id) for c in kept_after),
        escalated_candidate_ids=escalated,
        escalated_spans_mezz=escalated_spans,
        reviews=fused,
        commit_version=receipt.version,
        proposal_id=str(proposal.proposal_id),
        candidate_count=len(proposal.candidates),
        wall_seconds=time.monotonic() - started,
        hypothesis_segments_ms=hypothesis_ms,
        notes=tuple(notes),
    )


__all__ = [
    "ESCALATE_BELOW",
    "ArmPipelineData",
    "ArmPipelineError",
    "ArmPipelineInputs",
    "ArmPipelineResult",
    "VideoUnderstandingFactory",
    "run_arm_pipeline",
    "run_arm_stages",
]
