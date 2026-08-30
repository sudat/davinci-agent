"""Typed selection gates for the Director v2 passes (T4 split).

Extracted from ``director_v2`` once it crossed the 250 pure-LOC module
ceiling (same precedent as ``_v44_arm_integrity`` in the cli layer). Every
gate here is propose-only validation: it REFUSES a draft with a typed
``DirectorV2Error`` and never mutates the selection — the model (or the
heuristic in diagnostics) stays the only chooser of intent.

- ``validated_selection`` — model payload → parsed draft with unknown-id and
  uncorroborated-keep refusals (NO-INVENTED-IDS discipline, task 28).
- ``require_fused_keeps`` — keeps must cite overlapping fused deep-review
  evidence (T7).
- ``require_eligible_removals`` — removes must match the precomputed
  deterministic eligibility with cited evidence (T4 fail-closed).
- ``require_known_targets`` — creative intents may target known candidates
  only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.editorial_v2.prompt_v2 import (
    CreativeEditDraft,
    MomentSelectionDraft,
)
from services.editorial_v2.removal_policy import ineligible_removal_detail

if TYPE_CHECKING:
    from services.editorial_v2.evidence_v2 import EvidenceBundleV2
    from services.editorial_v2.removal_policy import RemovalEligibilityV1


class DirectorV2Error(ValueError):
    """Structured refusal from the director seam (never a silent partial)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def validated_selection(
    payload: object,
    known_candidates: frozenset[str],
    evidence: EvidenceBundleV2,
) -> MomentSelectionDraft:
    draft = MomentSelectionDraft.model_validate(payload)
    unknown = {c.candidate_id for c in draft.proposal.candidates} - known_candidates
    if unknown:
        raise DirectorV2Error(
            "unknown-candidate",
            f"selection cites candidates no api_v2 row produced: {sorted(unknown)}",
        )
    corroborated = {entry.candidate_id for entry in evidence.entries}
    unbacked = {
        c.candidate_id for c in draft.proposal.candidates
        if c.intent != "remove" and c.candidate_id not in corroborated
    }
    if unbacked:
        raise DirectorV2Error(
            "uncorroborated-keep",
            f"kept candidates lack evidence-bundle corroboration: {sorted(unbacked)}",
        )
    return draft


def require_fused_keeps(
    selection: MomentSelectionDraft, evidence: EvidenceBundleV2
) -> None:
    """Refuse keeps without overlapping fused moment-review citations."""

    cited = {entry.candidate_id: entry.moment_reviews for entry in evidence.entries}
    unbacked = sorted(
        candidate.candidate_id
        for candidate in selection.proposal.candidates
        if candidate.intent == "keep" and not cited.get(candidate.candidate_id)
    )
    if unbacked:
        raise DirectorV2Error(
            "uncorroborated-keep",
            f"kept candidates lack fused moment-review evidence (no overlapping "
            f"MomentDeepReviewV1 in the index): {unbacked}",
        )


def require_eligible_removals(
    selection: MomentSelectionDraft, eligibility: tuple[RemovalEligibilityV1, ...]
) -> None:
    """Refuse removes the precomputed eligibility does not back (T4)."""

    detail = ineligible_removal_detail(
        selection.proposal.candidates,
        {entry.candidate_id: entry for entry in eligibility},
    )
    if detail is not None:
        raise DirectorV2Error("removal-not-eligible", detail)


def require_known_targets(
    creative: CreativeEditDraft, known_candidates: frozenset[str]
) -> None:
    targets = {
        i.target_candidate_id
        for i in creative.intents
        if i.target_candidate_id is not None
    }
    unknown = targets - known_candidates
    if unknown:
        raise DirectorV2Error(
            "unknown-candidate", f"intents target unknown candidates: {sorted(unknown)}"
        )


__all__ = [
    "DirectorV2Error",
    "require_eligible_removals",
    "require_fused_keeps",
    "require_known_targets",
    "validated_selection",
]
