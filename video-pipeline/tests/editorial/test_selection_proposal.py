"""Todo 40 acceptance: deterministic Candidate IDs + Selection Plan Proposals.

Candidates are reconciled from the Todo-39 director proposal (STRUCTURE ONLY)
onto analyzer-backed records: every director-selected segment must resolve to
an evidence-backed candidate; IDs are ALWAYS recomputed from normalized
(source, span, intent, analyzer version) — never trusted from stored or
model-authored values; adjusted spans chain to parents; overlapping keeps get
deterministic redundancy groups; must-include forcing comes from the frozen
manifest rule spec. The model must never assign IDs or omit evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.analyze.audio_constants import (
    ANALYZER_VERSION,
    FALSE_START_RULE_ID,
    FILLER_RULE_ID,
)
from services.contracts.editorial_model import (
    EditorialSelectionProposal,
    SelectionEntry,
)
from services.contracts.primitives import ArtifactRef
from services.editorial.candidate_ids import (
    CandidateIdentityConflict,
    compute_candidate_id,
)
from services.editorial.candidate_models import (
    FROZEN_CANDIDATE_INTENTS,
    AnalyzerSegmentRecord,
    Candidate,
    CandidatePool,
    CandidateProvenance,
    CandidateSpan,
    EvidenceIndex,
    SegmentKind,
)
from services.editorial.proposal_builder import (
    ProposalBuildError,
    build_selection_proposal,
)
from services.editorial.reconcile import ReconcileError, ReconciliationResult, reconcile
from services.fixtures.manifest_phase1 import (
    MustIncludeRules,
    Phase1TechnicalFixtureManifest,
)
from services.foundation_io import canonical_model_bytes
from tests.editorial.support import (
    GOLDEN_EXPECTED_PATH,
    golden_proposal,
    load_manifest,
)

if TYPE_CHECKING:
    from services.editorial.proposal_builder import SelectionPlanProposal

ALL_FIXTURE_IDS = (
    "p1-ref-01-clean-ja",
    "p1-ref-02-pauses-fillers",
    "p1-ref-03-multi-take-must-include",
    "p1-ref-04-linked-av-offset",
    "p1-ref-05-review-mix",
)

RATE_NUM = 30
RATE_DEN = 1
T2A_SPAN = (510, 660)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _span(start_frame: int, end_frame: int) -> CandidateSpan:
    return CandidateSpan(
        start_frame=start_frame,
        end_frame=end_frame,
        rate_num=RATE_NUM,
        rate_den=RATE_DEN,
        start_ms=start_frame * 1000 // RATE_NUM,
        end_ms=end_frame * 1000 // RATE_NUM,
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
            artifact_id="transcript-asr-whisper-cpp",
            sha256=_sha(f"{fixture_id}:transcript"),
        ),
        ArtifactRef(
            artifact_id="analysis-dialogue-candidates",
            sha256=_sha(f"{fixture_id}:analysis"),
        ),
    )


def _record(  # noqa: PLR0913 (record fields are the fixture input)
    fixture_id: str,
    manifest: Phase1TechnicalFixtureManifest,
    segment_id: str,
    start_frame: int,
    end_frame: int,
    *,
    kind: SegmentKind = "speech",
    evidence: tuple[ArtifactRef, ...] | None = None,
    adjusted_from: str | None = None,
) -> AnalyzerSegmentRecord:
    return AnalyzerSegmentRecord(
        segment_id=segment_id,
        kind=kind,
        span=_span(start_frame, end_frame),
        evidence=evidence if evidence is not None else _evidence(fixture_id),
        provenance=CandidateProvenance(
            analyzer_version=ANALYZER_VERSION,
            rule_ids=(_rule_id(kind, manifest),),
        ),
        adjusted_from=adjusted_from,
    )


def pool_for(manifest: Phase1TechnicalFixtureManifest) -> CandidatePool:
    fixture_id = manifest.fixture_id
    records = [
        _record(
            fixture_id,
            manifest,
            segment.segment_id,
            segment.span.start_frame,
            segment.span.end_frame,
            kind=segment.kind,
        )
        for segment in manifest.transcript.segments
    ]
    kinds: dict[str, SegmentKind] = {
        segment.segment_id: segment.kind for segment in manifest.transcript.segments
    }
    for command in manifest.review_commands:
        if command.operation != "adjust_source_span" or command.new_span is None:
            continue
        assert command.target.item_id is not None
        records.append(
            _record(
                fixture_id,
                manifest,
                command.target.item_id,
                command.new_span.start_frame,
                command.new_span.end_frame,
                kind=kinds[command.target.item_id],
                adjusted_from=command.target.item_id,
            )
        )
    return CandidatePool(
        source_id=manifest.edit_source.source_id,
        edit_source_sha=_sha(f"{fixture_id}:edit-source"),
        total_frames=manifest.edit_source.total_frames,
        segments=tuple(records),
    )


def index_for(
    manifest: Phase1TechnicalFixtureManifest, *, edit_sha: str | None = None
) -> EvidenceIndex:
    fixture_id = manifest.fixture_id
    return EvidenceIndex(
        rows=_evidence(fixture_id),
        edit_source_sha=edit_sha if edit_sha is not None else _sha(f"{fixture_id}:edit-source"),
    )


def _document(fixture_id: str) -> dict[str, object]:
    return dict(golden_proposal(fixture_id).model_dump(mode="json"))


def _build(
    manifest: Phase1TechnicalFixtureManifest,
    document: object,
    *,
    must_include: tuple[str, ...] | None = None,
) -> SelectionPlanProposal:
    rules = manifest.editorial_rules
    if must_include is not None:
        rules = rules.model_copy(
            update={
                "must_include": MustIncludeRules(
                    rule_id=manifest.editorial_rules.must_include.rule_id,
                    segment_ids=must_include,
                )
            }
        )
    return build_selection_proposal(
        episode_id=manifest.fixture_id,
        rules=rules,
        pool=pool_for(manifest),
        director_document=document,
        evidence_index=index_for(manifest),
        plan_base_version="plan-base-v0",
        model_role_id="editorial-director",
        contract_version="phase-1-editorial-v1",
        fixture_only=True,
    )


def _golden_selection(fixture_id: str) -> tuple[set[str], set[str]]:
    document: object = json.loads(Path(GOLDEN_EXPECTED_PATH).read_bytes())
    assert isinstance(document, dict)
    fixtures = document["fixtures"]
    assert isinstance(fixtures, dict)
    entry = fixtures[fixture_id]
    assert isinstance(entry, dict)
    selection = entry["selection"]
    assert isinstance(selection, dict)
    selected, dropped = selection["selected_ids"], selection["dropped_ids"]
    assert isinstance(selected, list)
    assert isinstance(dropped, list)
    return {str(item) for item in selected}, {str(item) for item in dropped}


def _one_entry_proposal(
    fixture_id: str, segment_id: str, action: str, reason_code: str
) -> EditorialSelectionProposal:
    return golden_proposal(fixture_id).model_copy(update={"selection": (
        SelectionEntry(
            segment_id=segment_id, action=action, reason_code=reason_code  # type: ignore[arg-type]
        ),
    )})


def intents_by_segment(result: ReconciliationResult) -> dict[str, set[str]]:
    by_id = {candidate.candidate_id: candidate.intent for candidate in result.candidates}
    return {
        link.segment_id: {by_id[candidate_id] for candidate_id in link.candidate_ids}
        for link in result.segment_links
    }


# ---------------------------------------------------------------- happy paths


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_reconciled_candidates_are_stable_and_evidence_backed(fixture_id: str) -> None:
    manifest = load_manifest(fixture_id)
    pool = pool_for(manifest)
    index = index_for(manifest)
    proposal = golden_proposal(fixture_id)

    first = reconcile(proposal, pool, index)
    second = reconcile(proposal, pool, index)
    assert canonical_model_bytes(first) == canonical_model_bytes(second)

    ids = [candidate.candidate_id for candidate in first.candidates]
    assert len(set(ids)) == len(ids), "no duplicate or conflicting candidate identity"

    for candidate in first.candidates:
        assert candidate.evidence, "every candidate carries at least one evidence ref"
        assert candidate.provenance.rule_ids
        for ref in candidate.evidence:
            assert index.sha_for(ref.artifact_id) == ref.sha256
        # IDs are recomputed from normalized fields, never trusted from storage
        assert candidate.candidate_id == compute_candidate_id(
            edit_source_sha=candidate.source_ref.edit_source_sha,
            source_id=candidate.source_ref.source_id,
            start_frame=candidate.span.start_frame,
            end_frame=candidate.span.end_frame,
            rate_num=candidate.span.rate_num,
            rate_den=candidate.span.rate_den,
            intent=candidate.intent,
            analyzer_version=candidate.analyzer_version,
        )


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_reconciled_intents_match_golden_selection(fixture_id: str) -> None:
    manifest = load_manifest(fixture_id)
    result = reconcile(golden_proposal(fixture_id), pool_for(manifest), index_for(manifest))
    selected, dropped = _golden_selection(fixture_id)
    mapping = intents_by_segment(result)
    assert {seg for seg, intents in mapping.items() if "keep" in intents} == selected
    assert {seg for seg, intents in mapping.items() if "remove" in intents} == dropped


def test_adjusted_spans_create_parent_chains() -> None:
    fixture_id = "p1-ref-05-review-mix"
    manifest = load_manifest(fixture_id)
    result = reconcile(golden_proposal(fixture_id), pool_for(manifest), index_for(manifest))
    adjusted = [candidate for candidate in result.candidates if candidate.intent == "adjust"]
    assert len(adjusted) == 1
    child = adjusted[0]
    assert (child.span.start_frame, child.span.end_frame) == (300, 420)
    assert child.parent_candidate_id in set(result.ids_for("s3"))
    parent = next(
        candidate for candidate in result.candidates
        if candidate.candidate_id == child.parent_candidate_id
    )
    assert parent.intent == "keep"
    assert (parent.span.start_frame, parent.span.end_frame) == (300, 450)


def test_redundancy_groups_are_deterministic() -> None:
    # frozen fixtures declare adjacent (non-overlapping) keeps: no groups
    for fixture_id in ALL_FIXTURE_IDS:
        manifest = load_manifest(fixture_id)
        result = reconcile(
            golden_proposal(fixture_id), pool_for(manifest), index_for(manifest)
        )
        assert all(
            candidate.redundancy_group is None
            for candidate in result.candidates
            if candidate.intent == "keep"
        )
    # crafted overlapping keeps group deterministically
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    pool = CandidatePool(
        source_id=manifest.edit_source.source_id,
        edit_source_sha=_sha("overlap:edit-source"),
        total_frames=600,
        segments=(
            _record(fixture_id, manifest, "sA", 0, 200),
            _record(fixture_id, manifest, "sB", 100, 300),
            _record(fixture_id, manifest, "sC", 150, 350),
        ),
    )
    proposal = EditorialSelectionProposal(
        schema_version="editorial-selection-proposal-v1",
        proposal_id="sel-overlap-v1",
        episode_id=fixture_id,
        actor_intent="model",
        selection=(
            SelectionEntry(segment_id="sA", action="selected", reason_code="score-selected"),
            SelectionEntry(segment_id="sB", action="selected", reason_code="score-selected"),
            SelectionEntry(segment_id="sC", action="dropped", reason_code="below-min-score"),
        ),
        confidence=(2, 3),
    )
    index = EvidenceIndex(
        rows=_evidence(fixture_id),
        edit_source_sha=_sha("overlap:edit-source"),
    )
    first = reconcile(proposal, pool, index)
    second = reconcile(proposal, pool, index)
    assert canonical_model_bytes(first) == canonical_model_bytes(second)
    keeps = [candidate for candidate in first.candidates if candidate.intent == "keep"]
    groups = {candidate.redundancy_group for candidate in keeps}
    assert len(groups) == 1, "overlapping keeps share one deterministic redundancy group"
    expected_key = "rg-" + hashlib.sha256(
        json.dumps(sorted(c.candidate_id for c in keeps), separators=(",", ":")).encode()
    ).hexdigest()
    assert groups == {expected_key}
    dropped = [candidate for candidate in first.candidates if candidate.intent == "remove"]
    assert dropped
    assert dropped[0].redundancy_group is None


def test_builder_enforces_must_include() -> None:
    fixture_id = "p1-ref-03-multi-take-must-include"
    manifest = load_manifest(fixture_id)

    happy = _build(manifest, _document(fixture_id))
    happy_t2a = [
        candidate
        for candidate in happy.candidates
        if (candidate.span.start_frame, candidate.span.end_frame) == T2A_SPAN
    ]
    assert len(happy_t2a) == 1
    assert happy_t2a[0].intent == "keep"
    assert happy_t2a[0].candidate_id in set(happy.must_include)

    # a director that DROPPED the must-include segment is still forced
    entries = []
    for entry in golden_proposal(fixture_id).selection:
        action = "dropped" if entry.segment_id == "t2a" else entry.action
        entries.append(
            SelectionEntry(
                segment_id=entry.segment_id, action=action, reason_code=entry.reason_code
            )
        )
    forced = _build(
        manifest,
        EditorialSelectionProposal(
            schema_version="editorial-selection-proposal-v1",
            proposal_id="sel-t2a-dropped-v1",
            episode_id=fixture_id,
            actor_intent="model",
            selection=tuple(entries),
            confidence=(3, 8),
        ).model_dump(mode="json"),
    )
    forced_t2a = [
        candidate
        for candidate in forced.candidates
        if (candidate.span.start_frame, candidate.span.end_frame) == T2A_SPAN
    ]
    assert [candidate.intent for candidate in forced_t2a] == ["keep"]
    assert {candidate.candidate_id for candidate in forced_t2a} <= set(forced.must_include)

    # a must-include segment with no evidence-backed candidate is unresolved
    with pytest.raises(ProposalBuildError, match="must_include_unresolved"):
        _build(manifest, _document(fixture_id), must_include=("ghost",))


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_builder_produces_strict_proposal(fixture_id: str) -> None:
    manifest = load_manifest(fixture_id)
    first = _build(manifest, _document(fixture_id))
    second = _build(manifest, _document(fixture_id))
    assert canonical_model_bytes(first) == canonical_model_bytes(second)
    assert first.schema_version == "selection-plan-proposal-v1"
    assert first.episode_id == fixture_id
    assert first.budget == manifest.editorial_rules.duration_budget
    assert first.ordering == manifest.editorial_rules.ordering
    assert first.plan_base_version == "plan-base-v0"
    assert first.producer.model_role_id == "editorial-director"
    assert first.producer.contract_version == "phase-1-editorial-v1"
    assert first.fixture_only is True
    assert first.candidates
    keep_ids = {
        candidate.candidate_id
        for candidate in first.candidates
        if candidate.intent == "keep"
    }
    assert set(first.must_include) <= keep_ids


# ------------------------------------------------------------------- failures


def test_model_uuid_injection_is_rejected() -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    base = _document(fixture_id)
    selection = base["selection"]
    assert isinstance(selection, list)
    poisoned_documents = (
        {**base, "candidate_id": "cand-model-1"},
        {**base, "uuid": "6f9619ff-8b86-d011-b42d-00cf4fc964ff"},
        {**base, "selection": [{"id": "model-assigned", **entry} for entry in selection]},
    )
    for document in poisoned_documents:
        with pytest.raises(ProposalBuildError, match="model_assigned_id"):
            _build(manifest, document)


def test_absent_evidence_is_rejected() -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    with pytest.raises(ValidationError):
        _record(fixture_id, manifest, "s1", 0, 150, evidence=())
    missing = CandidatePool(
        source_id=manifest.edit_source.source_id,
        edit_source_sha=_sha(f"{fixture_id}:edit-source"),
        total_frames=600,
        segments=(
            _record(
                fixture_id,
                manifest,
                "s1",
                0,
                150,
                evidence=(
                    ArtifactRef(artifact_id="transcript-missing", sha256=_sha("missing")),
                ),
            ),
        ),
    )
    with pytest.raises(ReconcileError) as failure:
        reconcile(
            _one_entry_proposal(fixture_id, "s1", "selected", "score-selected"),
            missing,
            index_for(manifest),
        )
    assert failure.value.code == "unresolved_evidence"


def test_out_of_source_span_is_rejected() -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    beyond = CandidatePool(
        source_id=manifest.edit_source.source_id,
        edit_source_sha=_sha(f"{fixture_id}:edit-source"),
        total_frames=600,
        segments=(_record(fixture_id, manifest, "s1", 0, 700),),
    )
    with pytest.raises(ReconcileError) as failure:
        reconcile(
            _one_entry_proposal(fixture_id, "s1", "selected", "score-selected"),
            beyond,
            index_for(manifest),
        )
    assert failure.value.code == "out_of_source_span"


def test_identity_conflict_on_same_id_different_content() -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    alt_evidence = (ArtifactRef(artifact_id="transcript-alt", sha256=_sha("alt:transcript")),)
    pool = CandidatePool(
        source_id=manifest.edit_source.source_id,
        edit_source_sha=_sha(f"{fixture_id}:edit-source"),
        total_frames=600,
        segments=(
            _record(fixture_id, manifest, "s1", 0, 150),
            _record(fixture_id, manifest, "s2", 0, 150, evidence=alt_evidence),
        ),
    )
    index = EvidenceIndex(
        rows=(*_evidence(fixture_id), *alt_evidence),
        edit_source_sha=_sha(f"{fixture_id}:edit-source"),
    )
    proposal = EditorialSelectionProposal(
        schema_version="editorial-selection-proposal-v1",
        proposal_id="sel-conflict-v1",
        episode_id=fixture_id,
        actor_intent="model",
        selection=(
            SelectionEntry(segment_id="s1", action="selected", reason_code="score-selected"),
            SelectionEntry(segment_id="s2", action="selected", reason_code="score-selected"),
        ),
        confidence=(2, 2),
    )
    with pytest.raises(CandidateIdentityConflict, match="identity conflict"):
        reconcile(proposal, pool, index)


def test_unsupported_intent_is_rejected() -> None:
    assert set(FROZEN_CANDIDATE_INTENTS) == {"remove", "keep", "adjust", "subtitle"}
    payload: dict[str, object] = {
        "candidate_id": "0" * 64,
        "source_ref": {"source_id": "src", "edit_source_sha": "0" * 64},
        "span": {
            "start_frame": 0,
            "end_frame": 150,
            "rate_num": 30,
            "rate_den": 1,
            "start_ms": 0,
            "end_ms": 5000,
        },
        "intent": "enhance",
        "analyzer_version": ANALYZER_VERSION,
        "parent_candidate_id": None,
        "duration_frames": 150,
        "handles": {"head_frames": 0, "tail_frames": 0},
        "redundancy_group": None,
        "evidence": ({"artifact_id": "transcript-asr-whisper-cpp", "sha256": "0" * 64},),
        "provenance": {"analyzer_version": ANALYZER_VERSION, "rule_ids": ("p1-scoring-v1",)},
    }
    with pytest.raises(ValidationError, match="enhance"):
        Candidate.model_validate(payload)


def test_stale_edit_source_sha_is_refused() -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    stale = index_for(manifest, edit_sha=_sha("drifted:edit-source"))
    with pytest.raises(ReconcileError) as failure:
        reconcile(golden_proposal(fixture_id), pool_for(manifest), stale)
    assert failure.value.code == "stale_edit_source"


def test_unsupported_segment_reference_is_rejected() -> None:
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    hallucinated = _one_entry_proposal(fixture_id, "s999", "selected", "invented-evidence")
    with pytest.raises(ReconcileError) as failure:
        reconcile(hallucinated, pool_for(manifest), index_for(manifest))
    assert failure.value.code == "unsupported_segment"


def test_malformed_inputs_are_rejected() -> None:
    with pytest.raises(ValidationError):  # float frames are forbidden
        CandidateSpan(
            start_frame=0.5,  # type: ignore[arg-type]
            end_frame=150,
            rate_num=RATE_NUM,
            rate_den=RATE_DEN,
            start_ms=0,
            end_ms=5000,
        )
    with pytest.raises(ValidationError):  # inverted span
        _span(150, 0)
    with pytest.raises(ValidationError):  # wrong ms mirror
        CandidateSpan(
            start_frame=0,
            end_frame=150,
            rate_num=RATE_NUM,
            rate_den=RATE_DEN,
            start_ms=0,
            end_ms=9999,
        )
    fixture_id = "p1-ref-01-clean-ja"
    manifest = load_manifest(fixture_id)
    with pytest.raises(ProposalBuildError, match="parse_failed"):
        _build(manifest, "not-a-proposal-document")
    wrong_episode = {**_document(fixture_id), "episode_id": "another-episode"}
    with pytest.raises(ProposalBuildError, match="episode_mismatch"):
        _build(manifest, wrong_episode)
