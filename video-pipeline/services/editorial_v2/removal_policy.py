"""Deterministic removal eligibility — the v4.4 fail-closed cut policy.

Owner decision (frozen): a model may cut ONLY for a reason deterministic
code precomputed from the effective transcript — ``false_start`` (the frozen
analyzer rule identifies the same span) or ``exact_duplicate`` (adjacent
speech byte-equal after the shared Japanese normalization). Semantic
similarity, redundancy scores, embeddings, viewer-value prose, model
confidence, and LLM rationale NEVER grant eligibility.

``RemovalEligibilityV1`` is a RUNTIME-ONLY value object: it rides the Pass B
request and the commit-boundary validation as typed input, and is never
registered, persisted, or versioned as an authoritative artifact. Entries
are computed fresh from the current effective transcript on every run —
there is no stored permission to go stale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from services.editorial_v2.moment_models import RemovalReason

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from services.editorial_v2.moment_models import MomentCandidateV2

#: Mirrors services.toolchain.whisper_ja._normalize_japanese: punctuation,
#: brackets, and whitespace never count as transcript content — the one
#: existing Japanese normalization this policy reuses for byte-equality.
_STRIP_RE: Final = re.compile(r"[、。.,!?\s\u3000「」『』]")


@dataclass(frozen=True, slots=True)
class RemovalEligibilityV1:
    """Which deterministic removal reasons one candidate may legally carry.

    ``allowed_reasons`` empty means the candidate may not be removed at all;
    ``evidence_refs`` names the deterministic evidence a remove proposal MUST
    cite whenever a reason is allowed.
    """

    candidate_id: str
    allowed_reasons: frozenset[RemovalReason]
    evidence_refs: tuple[str, ...] = ()


def normalize_duplicate_text(text: str) -> str:
    """Transcript content with punctuation/brackets/whitespace removed."""
    return _STRIP_RE.sub("", text)


def ineligible_removal_detail(
    candidates: Sequence[MomentCandidateV2],
    eligibility: Mapping[str, RemovalEligibilityV1],
) -> str | None:
    """Detail of the first removal-policy violation, or ``None`` when every
    remove in the proposal is backed by the precomputed eligibility.

    Fail-closed order per remove candidate: no eligibility entry → no
    removal_reason → reason outside the allowed set → deterministic evidence
    not cited. Rationale text is never consulted.
    """
    for candidate in candidates:
        if candidate.intent != "remove":
            continue
        entry = eligibility.get(candidate.candidate_id)
        if entry is None:
            return (
                f"candidate {candidate.candidate_id} is proposed for remove but "
                "has no precomputed removal eligibility"
            )
        if candidate.removal_reason is None:
            return (
                f"candidate {candidate.candidate_id} is proposed for remove "
                "without a removal_reason"
            )
        if candidate.removal_reason not in entry.allowed_reasons:
            return (
                f"candidate {candidate.candidate_id} cites removal_reason "
                f"'{candidate.removal_reason}' outside its precomputed allowed "
                f"reasons {sorted(entry.allowed_reasons)}"
            )
        uncited = sorted(set(entry.evidence_refs) - set(candidate.evidence_refs))
        if uncited:
            return (
                f"candidate {candidate.candidate_id} does not cite its "
                f"deterministic removal evidence {uncited}"
            )
    return None


__all__ = [
    "RemovalEligibilityV1",
    "RemovalReason",
    "ineligible_removal_detail",
    "normalize_duplicate_text",
]
