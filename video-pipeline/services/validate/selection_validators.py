"""The LOCK / CAPABILITY / EPISODE-CONTRACT validators and the ordered run (Todo 41).

LOCK defers proposals that mutate a locked field of a locked span.
CAPABILITY maps every intent onto the frozen Phase-1 allowlist (bound
through the Phase-0A gate capabilities). EPISODE CONTRACT compares the
proposal's declared contract fields with the committed episode record
(Todo 32). ``run_selection_validators`` executes SCHEMA → SEMANTIC →
LOCK → CAPABILITY → EPISODE CONTRACT and returns the first refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.gates.models import GatePolicy
from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.validate.selection_models import (
    CommittedEpisodeRecord,
    SelectionLock,
    SelectionLockField,
    SelectionValidationError,
    ValidationContext,
    ValidationRefusal,
)
from services.validate.selection_schema import parse_selection_document
from services.validate.selection_semantic import chain_roots, validate_semantic

if TYPE_CHECKING:
    from services.editorial.candidate_models import (
        Candidate,
        CandidateIntent,
        SelectionPlanProposal,
    )

INTENT_LOCKED_FIELD: Final[dict[CandidateIntent, SelectionLockField]] = {
    "remove": "selection",
    "adjust": "source_span",
    "subtitle": "text",
}
INTENT_REQUIRED_CAPABILITY: Final[dict[CandidateIntent, str]] = {
    "remove": "base_cut",
    "keep": "base_cut",
    "adjust": "base_cut",
    "subtitle": "fixed_subtitle",
}
FROZEN_CONTRACT_ID: Final[str] = "talking-head-mvp-v1"


def validate_locks(
    proposal: SelectionPlanProposal, locks: tuple[SelectionLock, ...]
) -> ValidationRefusal | None:
    """A proposal mutating a locked field of a locked span defers, never commits."""

    if not locks:
        return None
    by_id = {candidate.candidate_id: candidate for candidate in proposal.candidates}
    for candidate in proposal.candidates:
        locked_field = INTENT_LOCKED_FIELD.get(candidate.intent)
        if locked_field is None:
            continue
        conflict = _lock_conflict(candidate, locked_field, by_id, locks)
        if conflict is not None:
            return conflict
    return None


def _lock_conflict(
    candidate: Candidate,
    locked_field: SelectionLockField,
    by_id: dict[str, Candidate],
    locks: tuple[SelectionLock, ...],
) -> ValidationRefusal | None:
    for member in chain_roots(candidate, by_id):
        for lock in locks:
            if lock.field != locked_field:
                continue
            same_source = (
                member.source_ref.source_id == lock.source_id
                and member.source_ref.edit_source_sha == lock.edit_source_sha
            )
            if same_source and member.span == lock.span:
                return ValidationRefusal(
                    validator="lock",
                    code="lock_conflict",
                    detail=(
                        f"candidate {candidate.candidate_id} mutates locked field "
                        f"{lock.field} of the span locked by {lock.grantor}"
                    ),
                    deferred=True,
                )
    return None


def validate_capabilities(
    proposal: SelectionPlanProposal, allowlist: tuple[str, ...]
) -> ValidationRefusal | None:
    """Every intent must map onto the frozen Phase-1 capability allowlist."""

    allowed = set(allowlist)
    for candidate in proposal.candidates:
        required = INTENT_REQUIRED_CAPABILITY.get(candidate.intent)
        if required is None or required not in allowed:
            return ValidationRefusal(
                validator="capability",
                code="capability_missing",
                detail=(
                    f"intent {candidate.intent} of candidate {candidate.candidate_id} "
                    f"requires capability {required!r} which is not in the frozen "
                    "Phase-1 allowlist"
                ),
            )
    return None


def validate_episode_contract(
    proposal: SelectionPlanProposal, record: CommittedEpisodeRecord
) -> ValidationRefusal | None:
    """The proposal's declared contract must match the committed episode record."""

    def mismatch(detail: str) -> ValidationRefusal:
        return ValidationRefusal(
            validator="episode_contract", code="episode_contract_mismatch", detail=detail
        )

    if proposal.episode_id != record.episode_id:
        return mismatch(
            f"proposal episode {proposal.episode_id} is not the committed episode "
            f"{record.episode_id}"
        )
    if record.contract_id != FROZEN_CONTRACT_ID:
        return mismatch(
            f"episode contract {record.contract_id} is not the frozen supported "
            f"contract {FROZEN_CONTRACT_ID}"
        )
    if record.status != "supported":
        return mismatch(
            f"episode status {record.status!r} is outside the Supported Episode Contract"
        )
    if record.privacy_flags or record.rights_flags:
        return mismatch(
            "declared privacy/rights flags require their human gates before any "
            "automated selection commit"
        )
    if proposal.fixture_only != record.fixture_only:
        return mismatch(
            f"proposal fixture_only={proposal.fixture_only} contradicts the committed "
            f"episode record fixture_only={record.fixture_only}"
        )
    return None


def frozen_phase1_capability_allowlist(policy_path: Path) -> tuple[str, ...]:
    """Resolve the frozen Phase-1 policy capabilities (bound through Phase 0A)."""

    policy = GatePolicy.model_validate_json(policy_path.read_bytes())
    if policy.gate_id != "phase-1-technical":
        raise ValueError(
            f"expected the phase-1-technical gate policy, got {policy.gate_id}"
        )
    return tuple(PHASE_0A_CAPABILITIES)


def evidence_refs(proposal: SelectionPlanProposal) -> tuple[tuple[str, str], ...]:
    """Deduplicated (artifact_id, sha256) evidence rows across all candidates."""

    rows: dict[str, str] = {}
    for candidate in proposal.candidates:
        for ref in candidate.evidence:
            rows.setdefault(ref.artifact_id, ref.sha256)
    return tuple(sorted(rows.items()))


def run_selection_validators(
    document: object, context: ValidationContext
) -> tuple[SelectionPlanProposal | None, ValidationRefusal | None]:
    """Run SCHEMA → SEMANTIC → LOCK → CAPABILITY → EPISODE CONTRACT in order."""

    try:
        proposal = parse_selection_document(document)
    except SelectionValidationError as error:
        return None, error.refusal
    for refusal in (
        validate_semantic(proposal, context),
        validate_locks(proposal, context.locks),
        validate_capabilities(proposal, context.capability_allowlist),
        validate_episode_contract(proposal, context.episode),
    ):
        if refusal is not None:
            return proposal, refusal
    return proposal, None


__all__ = [
    "FROZEN_CONTRACT_ID",
    "INTENT_LOCKED_FIELD",
    "INTENT_REQUIRED_CAPABILITY",
    "evidence_refs",
    "frozen_phase1_capability_allowlist",
    "run_selection_validators",
    "validate_capabilities",
    "validate_episode_contract",
    "validate_locks",
    "validate_semantic",
]
