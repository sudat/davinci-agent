"""Strict Selection Plan Proposal assembly (Todo 40).

``build_selection_proposal`` guards the director document (the model may NEVER
assign ids — any ``id``/``uuid``/``candidate_id`` key is rejected), re-parses
it through the frozen Todo-39 strict parser, reconciles it onto evidence-
backed analyzer candidates, forces the manifest must-include set (a dropped or
omitted must-include segment becomes a keep candidate; an unresolvable one is
a hard error), and assembles the strict proposal envelope. The result is a
proposal only — never a commit, plan mutation, or job-state change.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from services.editorial.candidate_models import (
    Candidate,
    CandidatePool,
    CandidateSourceRef,
    EvidenceIndex,
    ProposalProducer,
    SelectionPlanProposal,
    derive_candidate,
)
from services.editorial.models import EditorialErrorRecord
from services.editorial.parse import parse_response
from services.editorial.reconcile import (
    ReconciliationResult,
    base_record,
    finalize_candidates,
    reconcile,
    require_in_source_span,
    require_resolved_evidence,
)

if TYPE_CHECKING:
    from services.contracts.editorial_model import EditorialSelectionProposal
    from services.fixtures.manifest_phase1 import EditorialRules

ProposalBuildErrorCode = Literal[
    "model_assigned_id",
    "parse_failed",
    "episode_mismatch",
    "must_include_unresolved",
]


class ProposalBuildError(Exception):
    """The proposal could not be assembled under the strict contract."""

    def __init__(self, code: ProposalBuildErrorCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _reject_model_assigned_ids(node: object) -> None:
    stack: list[object] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if isinstance(key, str):
                    lowered = key.lower()
                    if lowered == "id" or "uuid" in lowered or "candidate_id" in lowered:
                        raise ProposalBuildError(
                            "model_assigned_id",
                            f"director input key {key!r} carries a model-assigned "
                            "identifier; candidate IDs are derived deterministically "
                            "and never accepted from the model",
                        )
                stack.append(child)
        elif isinstance(current, list | tuple):
            stack.extend(current)


def _flip_to_keep(candidate: Candidate) -> Candidate:
    return derive_candidate(
        source_ref=candidate.source_ref,
        span=candidate.span,
        intent="keep",
        analyzer_version=candidate.analyzer_version,
        evidence=candidate.evidence,
        provenance=candidate.provenance,
        handles=candidate.handles,
    )


def _force_must_include(
    result: ReconciliationResult,
    segment_ids: tuple[str, ...],
    pool: CandidatePool,
    evidence_index: EvidenceIndex,
) -> tuple[tuple[Candidate, ...], tuple[str, ...]]:
    """Force every manifest must-include segment to a keep candidate."""

    candidates: list[Candidate] = list(result.candidates)
    must_ids: list[str] = []
    for segment_id in segment_ids:
        linked = [
            candidate
            for candidate in candidates
            if candidate.candidate_id in set(result.ids_for(segment_id))
        ]
        keep = next(
            (candidate for candidate in linked if candidate.intent == "keep"), None
        )
        if keep is not None:
            must_ids.append(keep.candidate_id)
            continue
        drop = next(
            (candidate for candidate in linked if candidate.intent == "remove"), None
        )
        if drop is not None:
            forced = _flip_to_keep(drop)
            candidates[candidates.index(drop)] = forced
            must_ids.append(forced.candidate_id)
            continue
        record = base_record(pool, segment_id)
        if record is None:
            raise ProposalBuildError(
                "must_include_unresolved",
                f"must-include segment {segment_id} has no evidence-backed analyzer "
                "candidate; forcing cannot invent evidence",
            )
        require_in_source_span(record, pool)
        require_resolved_evidence(record, evidence_index)
        created = derive_candidate(
            source_ref=CandidateSourceRef(
                source_id=pool.source_id, edit_source_sha=pool.edit_source_sha
            ),
            span=record.span,
            intent="keep",
            analyzer_version=record.provenance.analyzer_version,
            evidence=record.evidence,
            provenance=record.provenance,
        )
        candidates.append(created)
        must_ids.append(created.candidate_id)
    return finalize_candidates(candidates), tuple(sorted(set(must_ids)))


def build_selection_proposal(  # noqa: PLR0913 (frozen builder contract inputs)
    *,
    episode_id: str,
    rules: EditorialRules,
    pool: CandidatePool,
    director_document: object,
    evidence_index: EvidenceIndex,
    plan_base_version: str,
    model_role_id: str,
    contract_version: str,
    fixture_only: bool,
) -> SelectionPlanProposal:
    """Assemble one strict proposal; every failure is a classified error."""

    _reject_model_assigned_ids(director_document)
    try:
        payload = json.dumps(
            director_document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProposalBuildError(
            "parse_failed", f"director document is not JSON-serializable: {error}"
        ) from error
    parsed = parse_response(payload)
    if isinstance(parsed, EditorialErrorRecord):
        raise ProposalBuildError("parse_failed", f"{parsed.code}: {parsed.detail}")
    proposal: EditorialSelectionProposal = parsed.proposal
    if proposal.episode_id != episode_id:
        raise ProposalBuildError(
            "episode_mismatch",
            f"proposal episode {proposal.episode_id} does not match {episode_id}",
        )
    result = reconcile(proposal, pool, evidence_index)
    candidates, must_ids = _force_must_include(
        result, tuple(rules.must_include.segment_ids), pool, evidence_index
    )
    return SelectionPlanProposal(
        proposal_id=f"spp.{episode_id}.{plan_base_version}",
        episode_id=episode_id,
        candidates=candidates,
        must_include=must_ids,
        budget=rules.duration_budget,
        ordering=rules.ordering,
        plan_base_version=plan_base_version,
        producer=ProposalProducer(
            model_role_id=model_role_id, contract_version=contract_version
        ),
        fixture_only=fixture_only,
    )


__all__ = [
    "ProposalBuildError",
    "ProposalBuildErrorCode",
    "build_selection_proposal",
]
