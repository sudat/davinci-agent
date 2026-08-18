"""Service-side deterministic planning rig for the Phase-1 fixture chain.

Mirrors the frozen derivation the test rigs use (Todo 40/42/43): the candidate
pool and evidence index derive ONLY from the fixture manifest — declared
transcript segments (plus declared review-span adjustments), declared evidence
references, declared A/V link bindings, declared scoring weights and budget.
No golden-table reads, no clocks, no network: identical manifests always
produce identical pools, planner inputs, plans, and refs.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

from services.analyze.audio_constants import FALSE_START_RULE_ID, FILLER_RULE_ID
from services.contracts.primitives import ArtifactRef
from services.editorial.candidate_models import (
    AnalyzerSegmentRecord,
    CandidatePool,
    CandidateProvenance,
    CandidateSpan,
    EvidenceIndex,
    ProposalProducer,
    SegmentKind,
)
from services.editorial.proposal_builder import build_selection_proposal
from services.editorial.reconcile import reconcile
from services.plan.edit_plan_models import AudioSpanBinding
from services.validate.selection_models import EditSourceFacts

if TYPE_CHECKING:
    from services.contracts.editorial_model import EditorialSelectionProposal
    from services.editorial.candidate_models import SelectionPlanProposal
    from services.editorial.reconcile import ReconciliationResult
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest

SELECTION_BASE: Final = "plan-base-v0"
EDIT_BASE: Final = "edit-base-v0"
CHAIN_PRODUCER: Final = ProposalProducer(
    model_role_id="constraint-planner", contract_version="phase1-chain-v1"
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _span(start_frame: int, end_frame: int) -> CandidateSpan:
    return CandidateSpan(
        start_frame=start_frame,
        end_frame=end_frame,
        rate_num=30,
        rate_den=1,
        start_ms=start_frame * 1000 // 30,
        end_ms=end_frame * 1000 // 30,
    )


def _rule_id(kind: SegmentKind, manifest: Phase1TechnicalFixtureManifest) -> str:
    if kind == "speech":
        return manifest.editorial_rules.scoring.rule_id
    if kind == "pause":
        return manifest.editorial_rules.pauses.rule_id
    if kind == "filler":
        return FILLER_RULE_ID
    return FALSE_START_RULE_ID


def _evidence(fixture_id: str) -> tuple[ArtifactRef, ...]:
    return (
        ArtifactRef(
            artifact_id="transcript-asr-whisper-cpp", sha256=_sha(f"{fixture_id}:transcript")
        ),
        ArtifactRef(
            artifact_id="analysis-dialogue-candidates", sha256=_sha(f"{fixture_id}:analysis")
        ),
    )


def pool_for(manifest: Phase1TechnicalFixtureManifest) -> CandidatePool:
    fixture_id = manifest.fixture_id
    records = [
        AnalyzerSegmentRecord(
            segment_id=segment.segment_id,
            kind=segment.kind,
            span=_span(segment.span.start_frame, segment.span.end_frame),
            evidence=_evidence(fixture_id),
            provenance=CandidateProvenance(
                analyzer_version="todo34-v1", rule_ids=(_rule_id(segment.kind, manifest),)
            ),
        )
        for segment in manifest.transcript.segments
    ]
    kinds: dict[str, SegmentKind] = {
        segment.segment_id: segment.kind for segment in manifest.transcript.segments
    }
    for command in manifest.review_commands:
        if command.operation != "adjust_source_span" or command.new_span is None:
            continue
        if command.target.item_id is not None and command.target.item_id in kinds:
            records.append(
                AnalyzerSegmentRecord(
                    segment_id=command.target.item_id,
                    kind=kinds[command.target.item_id],
                    span=_span(command.new_span.start_frame, command.new_span.end_frame),
                    evidence=_evidence(fixture_id),
                    provenance=CandidateProvenance(
                        analyzer_version="todo34-v1", rule_ids=(_rule_id("speech", manifest),)
                    ),
                    adjusted_from=command.target.item_id,
                )
            )
    return CandidatePool(
        source_id=manifest.edit_source.source_id,
        edit_source_sha=_sha(f"{fixture_id}:edit-source"),
        total_frames=manifest.edit_source.total_frames,
        segments=tuple(records),
    )


def index_for(manifest: Phase1TechnicalFixtureManifest) -> EvidenceIndex:
    return EvidenceIndex(
        rows=_evidence(manifest.fixture_id),
        edit_source_sha=_sha(f"{manifest.fixture_id}:edit-source"),
    )


def edit_source_facts(manifest: Phase1TechnicalFixtureManifest) -> EditSourceFacts:
    pool = pool_for(manifest)
    return EditSourceFacts(
        source_id=pool.source_id,
        edit_source_sha=pool.edit_source_sha,
        total_frames=pool.total_frames,
    )


def selection_proposal_from(
    manifest: Phase1TechnicalFixtureManifest, director: EditorialSelectionProposal
) -> SelectionPlanProposal:
    return build_selection_proposal(
        episode_id=manifest.fixture_id,
        rules=manifest.editorial_rules,
        pool=pool_for(manifest),
        director_document=director.model_dump(mode="json"),
        evidence_index=index_for(manifest),
        plan_base_version=SELECTION_BASE,
        model_role_id="editorial-director",
        contract_version="phase-1-editorial-v1",
        fixture_only=True,
    )


def reconciled_for(
    manifest: Phase1TechnicalFixtureManifest, director: EditorialSelectionProposal
) -> ReconciliationResult:
    return reconcile(director, pool_for(manifest), index_for(manifest))


def _segment_map(reconciled: ReconciliationResult) -> dict[str, str]:
    return {
        candidate_id: link.segment_id
        for link in reconciled.segment_links
        for candidate_id in link.candidate_ids
    }


def audio_bindings_for(
    manifest: Phase1TechnicalFixtureManifest,
    selection: SelectionPlanProposal,
    reconciled: ReconciliationResult,
) -> tuple[AudioSpanBinding, ...]:
    mapping = _segment_map(reconciled)
    link_by_segment = {link.segment_id: link for link in manifest.av_links}
    return tuple(
        AudioSpanBinding(
            candidate_id=candidate.candidate_id,
            span=_span(
                link_by_segment[mapping[candidate.candidate_id]].audio_span.start_frame,
                link_by_segment[mapping[candidate.candidate_id]].audio_span.end_frame,
            ),
        )
        for candidate in selection.candidates
        if candidate.intent == "keep"
        and mapping.get(candidate.candidate_id) in link_by_segment
    )


__all__ = [
    "EDIT_BASE",
    "SELECTION_BASE",
    "edit_source_facts",
    "index_for",
    "pool_for",
    "reconciled_for",
    "selection_proposal_from",
]
