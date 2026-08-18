"""Reconcile director proposal structure onto analyzer candidates (Todo 40).

The Todo-39 director proposal is STRUCTURE ONLY (segment ids + actions). Every
selected or dropped segment must resolve to an evidence-backed analyzer record
(hallucinated references fail); spans must live inside the edit-source extent;
the evidence index must bind the same edit-source sha (stale drift fails) and
resolve every evidence ref; adjusted spans become child candidates chained to
their base; identical normalized candidates merge to one ID while same-ID /
different-content raises a hard identity conflict; overlapping keeps receive
deterministic redundancy groups. Reconciliation never guesses.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.editorial.candidate_ids import CandidateIdentityConflict
from services.editorial.candidate_models import (
    AnalyzerSegmentRecord,
    Candidate,
    CandidateIntent,
    CandidatePool,
    CandidateSourceRef,
    EvidenceIndex,
    derive_candidate,
)
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.contracts.editorial_model import EditorialSelectionProposal

ReconcileErrorCode = Literal[
    "unsupported_segment",
    "out_of_source_span",
    "stale_edit_source",
    "unresolved_evidence",
]


class ReconcileError(Exception):
    """A director selection could not be reconciled onto evidence-backed inputs."""

    def __init__(self, code: ReconcileErrorCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class SegmentLink(StrictModel):
    segment_id: Identifier
    candidate_ids: tuple[Sha256, ...] = Field(min_length=1)


class ReconciliationResult(StrictModel):
    """Deterministic reconciled candidates plus per-segment id linkage."""

    candidates: tuple[Candidate, ...] = Field(min_length=1)
    segment_links: tuple[SegmentLink, ...] = Field(min_length=1)

    def ids_for(self, segment_id: str) -> tuple[Sha256, ...]:
        for link in self.segment_links:
            if link.segment_id == segment_id:
                return link.candidate_ids
        return ()


class _IdentityLedger:
    """Same ID + same content merges; same ID + different content conflicts."""

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}

    def register(self, candidate: Candidate) -> Candidate:
        content = canonical_model_bytes(candidate)
        seen = self._content.get(candidate.candidate_id)
        if seen is None:
            self._content[candidate.candidate_id] = content
            return candidate
        if seen != content:
            raise CandidateIdentityConflict(
                f"identity conflict on candidate {candidate.candidate_id}: the same "
                "normalized (source, span, intent, analyzer version) carried different "
                "content — identical normalized fields merge, differing content is fatal"
            )
        return candidate


def require_in_source_span(record: AnalyzerSegmentRecord, pool: CandidatePool) -> None:
    span = record.span
    if span.end_frame > pool.total_frames or span.start_frame >= pool.total_frames:
        raise ReconcileError(
            "out_of_source_span",
            f"segment {record.segment_id} span [{span.start_frame},{span.end_frame}) "
            f"exceeds the edit source extent [0,{pool.total_frames})",
        )


def require_resolved_evidence(
    record: AnalyzerSegmentRecord, evidence_index: EvidenceIndex
) -> None:
    for ref in record.evidence:
        resolved = evidence_index.sha_for(ref.artifact_id)
        if resolved is None or resolved != ref.sha256:
            raise ReconcileError(
                "unresolved_evidence",
                f"segment {record.segment_id} evidence artifact {ref.artifact_id} "
                "does not resolve in the evidence index; candidates cannot omit or "
                "invent evidence",
            )


def base_record(pool: CandidatePool, segment_id: str) -> AnalyzerSegmentRecord | None:
    for record in pool.segments:
        if record.adjusted_from is None and record.segment_id == segment_id:
            return record
    return None


def _redundancy_key(member_ids: Sequence[str]) -> str:
    canonical = json.dumps(sorted(member_ids), separators=(",", ":")).encode("utf-8")
    return "rg-" + hashlib.sha256(canonical).hexdigest()


def _rebuild(candidate: Candidate, redundancy_group: str | None) -> Candidate:
    return derive_candidate(
        source_ref=candidate.source_ref,
        span=candidate.span,
        intent=candidate.intent,
        analyzer_version=candidate.analyzer_version,
        evidence=candidate.evidence,
        provenance=candidate.provenance,
        parent_candidate_id=candidate.parent_candidate_id,
        handles=candidate.handles,
        redundancy_group=redundancy_group,
    )


def assign_redundancy_groups(candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    """Group overlapping keep-intents under one deterministic key (idempotent)."""

    keeps = [candidate for candidate in candidates if candidate.intent == "keep"]
    parent = list(range(len(keeps)))

    def find(index: int) -> int:
        root = index
        while parent[root] != root:
            root = parent[root]
        while parent[index] != root:
            parent[index], index = root, parent[index]
        return root

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for i in range(len(keeps)):
        for j in range(i + 1, len(keeps)):
            if keeps[i].span.overlaps(keeps[j].span):
                union(i, j)
    members: dict[int, list[str]] = {}
    for index in range(len(keeps)):
        members.setdefault(find(index), []).append(keeps[index].candidate_id)
    keys = {root: _redundancy_key(ids) for root, ids in members.items() if len(ids) > 1}
    by_id: dict[str, str] = {
        member: keys[root]
        for root, ids in members.items()
        if root in keys
        for member in ids
    }
    return tuple(
        _rebuild(
            candidate,
            by_id.get(candidate.candidate_id) if candidate.intent == "keep" else None,
        )
        for candidate in candidates
    )


def _sort_key(candidate: Candidate) -> tuple[int, int, str, str]:
    return (
        candidate.span.start_frame,
        candidate.span.end_frame,
        candidate.intent,
        candidate.candidate_id,
    )


def finalize_candidates(candidates: Sequence[Candidate]) -> tuple[Candidate, ...]:
    """Regroup redundancies and order deterministically (source-order stable)."""

    return tuple(sorted(assign_redundancy_groups(tuple(candidates)), key=_sort_key))


def reconcile(
    director_proposal: EditorialSelectionProposal,
    analyzer_candidates: CandidatePool,
    evidence_index: EvidenceIndex,
) -> ReconciliationResult:
    """Map the frozen director proposal structure onto evidence-backed candidates."""

    if evidence_index.edit_source_sha != analyzer_candidates.edit_source_sha:
        raise ReconcileError(
            "stale_edit_source",
            "the evidence index binds a different edit-source sha than the analyzer "
            "candidate pool; refusing to reconcile over drifted inputs",
        )
    bases: dict[str, AnalyzerSegmentRecord] = {}
    adjustments: dict[str, list[AnalyzerSegmentRecord]] = {}
    for record in analyzer_candidates.segments:
        if record.adjusted_from is None:
            bases[record.segment_id] = record
        else:
            adjustments.setdefault(record.adjusted_from, []).append(record)
    source_ref = CandidateSourceRef(
        source_id=analyzer_candidates.source_id,
        edit_source_sha=analyzer_candidates.edit_source_sha,
    )
    ledger = _IdentityLedger()
    unique: dict[str, Candidate] = {}
    links: dict[str, list[str]] = {}

    def emit(
        record: AnalyzerSegmentRecord, intent: CandidateIntent, parent: str | None
    ) -> Candidate:
        require_in_source_span(record, analyzer_candidates)
        require_resolved_evidence(record, evidence_index)
        candidate = ledger.register(
            derive_candidate(
                source_ref=source_ref,
                span=record.span,
                intent=intent,
                analyzer_version=record.provenance.analyzer_version,
                evidence=record.evidence,
                provenance=record.provenance,
                parent_candidate_id=parent,
            )
        )
        unique.setdefault(candidate.candidate_id, candidate)
        segment_ids = links.setdefault(record.segment_id, [])
        if candidate.candidate_id not in segment_ids:
            segment_ids.append(candidate.candidate_id)
        return candidate

    for entry in director_proposal.selection:
        record = bases.get(entry.segment_id)
        if record is None:
            raise ReconcileError(
                "unsupported_segment",
                f"director-selected segment {entry.segment_id} has no evidence-backed "
                "analyzer candidate (hallucinated or unsupported reference)",
            )
        base = emit(record, "keep" if entry.action == "selected" else "remove", None)
        if entry.action == "selected":
            for adjusted in adjustments.get(entry.segment_id, ()):
                emit(adjusted, "adjust", base.candidate_id)

    return ReconciliationResult(
        candidates=finalize_candidates(tuple(unique.values())),
        segment_links=tuple(
            SegmentLink(segment_id=segment_id, candidate_ids=tuple(segment_ids))
            for segment_id, segment_ids in sorted(links.items())
        ),
    )


__all__ = [
    "ReconcileError",
    "ReconcileErrorCode",
    "ReconciliationResult",
    "SegmentLink",
    "assign_redundancy_groups",
    "base_record",
    "finalize_candidates",
    "reconcile",
    "require_in_source_span",
    "require_resolved_evidence",
]
