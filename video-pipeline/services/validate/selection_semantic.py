"""The SEMANTIC validator: recompute everything the proposal claims (Todo 41).

Candidate identities are recomputed from normalized fields, spans must
live inside the edit-source extent, parent chains must resolve and be
acyclic, redundancy groups must equal the deterministic recomputation,
must-include candidates must be kept, and one span may not carry both
keep and remove intents without a parent relation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.editorial.candidate_ids import compute_candidate_id
from services.editorial.reconcile import assign_redundancy_groups
from services.validate.selection_models import (
    EditSourceFacts,
    ValidationContext,
    ValidationRefusal,
)

if TYPE_CHECKING:
    from services.editorial.candidate_models import Candidate, SelectionPlanProposal

type SpanKey = tuple[str, str, int, int, int, int]


def _semantic_refusal(code: str, detail: str) -> ValidationRefusal:
    return ValidationRefusal(validator="semantic", code=code, detail=detail)


def _span_key(candidate: Candidate) -> SpanKey:
    return (
        candidate.source_ref.source_id,
        candidate.source_ref.edit_source_sha,
        candidate.span.start_frame,
        candidate.span.end_frame,
        candidate.span.rate_num,
        candidate.span.rate_den,
    )


def _check_candidate_shape(
    candidates: tuple[Candidate, ...], facts: EditSourceFacts
) -> ValidationRefusal | None:
    for candidate in candidates:
        expected = compute_candidate_id(
            edit_source_sha=candidate.source_ref.edit_source_sha,
            source_id=candidate.source_ref.source_id,
            start_frame=candidate.span.start_frame,
            end_frame=candidate.span.end_frame,
            rate_num=candidate.span.rate_num,
            rate_den=candidate.span.rate_den,
            intent=candidate.intent,
            analyzer_version=candidate.analyzer_version,
        )
        if candidate.candidate_id != expected:
            return _semantic_refusal(
                "identity_mismatch",
                f"candidate {candidate.candidate_id} does not equal its recomputed "
                "deterministic identity",
            )
        if (
            candidate.source_ref.source_id != facts.source_id
            or candidate.source_ref.edit_source_sha != facts.edit_source_sha
        ):
            return _semantic_refusal(
                "source_binding_mismatch",
                f"candidate {candidate.candidate_id} binds a different edit source "
                "than the validation context",
            )
        if (
            candidate.span.end_frame > facts.total_frames
            or candidate.span.start_frame >= facts.total_frames
        ):
            return _semantic_refusal(
                "span_out_of_extent",
                f"candidate {candidate.candidate_id} span exceeds the edit source "
                f"extent [0,{facts.total_frames})",
            )
    return None


def _check_parent_chains(
    candidates: tuple[Candidate, ...], by_id: dict[str, Candidate]
) -> ValidationRefusal | None:
    for candidate in candidates:
        seen: set[str] = set()
        current = candidate
        while current.parent_candidate_id is not None:
            if current.candidate_id in seen:
                return _semantic_refusal(
                    "parent_cycle", f"candidate {candidate.candidate_id} parent chain cycles"
                )
            seen.add(current.candidate_id)
            parent = by_id.get(current.parent_candidate_id)
            if parent is None:
                return _semantic_refusal(
                    "parent_unresolved",
                    f"candidate {candidate.candidate_id} references parent "
                    f"{current.parent_candidate_id} absent from the proposal",
                )
            current = parent
    return None


def _check_redundancy(candidates: tuple[Candidate, ...]) -> ValidationRefusal | None:
    rebuilt = assign_redundancy_groups(candidates)
    expected_groups = {item.candidate_id: item.redundancy_group for item in rebuilt}
    for candidate in candidates:
        if candidate.redundancy_group != expected_groups.get(candidate.candidate_id):
            return _semantic_refusal(
                "redundancy_inconsistent",
                f"candidate {candidate.candidate_id} redundancy group does not match "
                "the deterministic recomputation",
            )
    return None


def _check_must_include(
    proposal: SelectionPlanProposal, context: ValidationContext
) -> ValidationRefusal | None:
    keeps = {
        candidate.candidate_id
        for candidate in proposal.candidates
        if candidate.intent == "keep"
    }
    for must_id in proposal.must_include:
        if must_id not in keeps:
            return _semantic_refusal(
                "must_include_missing", f"must-include candidate {must_id} is not kept"
            )
    for mandatory in context.mandatory_candidate_ids:
        if mandatory not in keeps:
            return _semantic_refusal(
                "must_include_missing",
                f"mandatory candidate {mandatory} from the frozen rules is not kept",
            )
    return None


def _check_contradictions(candidates: tuple[Candidate, ...]) -> ValidationRefusal | None:
    by_span: dict[SpanKey, list[Candidate]] = {}
    for candidate in candidates:
        by_span.setdefault(_span_key(candidate), []).append(candidate)
    for group in by_span.values():
        keep = next((item for item in group if item.intent == "keep"), None)
        removed = next((item for item in group if item.intent == "remove"), None)
        if keep is None or removed is None:
            continue
        related = removed.parent_candidate_id == keep.candidate_id or (
            keep.parent_candidate_id == removed.candidate_id
        )
        if not related:
            return _semantic_refusal(
                "keep_remove_contradiction",
                f"span {_span_key(removed)} carries both keep and remove intents "
                "without a parent relation",
            )
    return None


def chain_roots(candidate: Candidate, by_id: dict[str, Candidate]) -> list[Candidate]:
    """The candidate plus every resolvable ancestor, stopping at cycles."""

    chain: list[Candidate] = []
    seen: set[str] = set()
    current = candidate
    while current.parent_candidate_id is not None:
        if current.candidate_id in seen:
            break
        seen.add(current.candidate_id)
        parent = by_id.get(current.parent_candidate_id)
        if parent is None:
            break
        chain.append(parent)
        current = parent
    return [candidate, *chain]


def validate_semantic(
    proposal: SelectionPlanProposal, context: ValidationContext
) -> ValidationRefusal | None:
    """Recompute identities, extents, chains, groups, and contradictions."""

    candidates = proposal.candidates
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    for refusal in (
        _check_candidate_shape(candidates, context.edit_source),
        _check_parent_chains(candidates, by_id),
        _check_redundancy(candidates),
        _check_must_include(proposal, context),
        _check_contradictions(candidates),
    ):
        if refusal is not None:
            return refusal
    return None


__all__ = [
    "SpanKey",
    "chain_roots",
    "validate_semantic",
]
