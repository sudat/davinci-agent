"""Kept-span/escalation derivation for the Arm pipeline result (T4 split).

Extracted from ``v44_arm_pipeline`` to respect the 250-LOC module ceiling
(same precedent as ``_v44_arm_integrity``/``_v44_arm_transcript``). T4
policy: kept spans preserve EVERY ``keep`` AND ``optional`` candidate —
optional is never silently dropped — while the fused-review low-confidence
escalation still applies ONLY to explicit keeps: optional is preserved by
owner policy, so demoting it on review confidence would reintroduce the
silent drop this task removes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

    from services.editorial_v2.evidence_v2 import EvidenceBundleV2
    from services.editorial_v2.moment_models import MomentCandidateV2

#: Arm-B escalation policy: a kept candidate whose OVERLAPPING fused review
#: confidence falls below this is DEMOTED from the kept spans and ESCALATED
#: (recorded, never silently kept nor silently dropped). T7 moves the
#: evidence BEFORE the Director; the policy application stays explicit.
ESCALATE_BELOW: Final = 0.5


@dataclass(frozen=True, slots=True)
class KeptSpans:
    """The kept/escalated facts the Arm result and report derive from."""

    kept_spans_mezz: tuple[tuple[int, int], ...]
    kept_candidate_ids: tuple[str, ...]
    escalated_candidate_ids: tuple[str, ...]
    escalated_spans_mezz: tuple[tuple[int, int], ...]


def unconfirmed_keeps(
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


def derive_kept_spans(
    bundle: EvidenceBundleV2, candidates: Sequence[MomentCandidateV2]
) -> KeptSpans:
    """Preserve every keep and optional candidate; escalate only explicit
    keeps whose fused-review confidence fell below the threshold."""
    kept = [c for c in candidates if c.intent in ("keep", "optional")]
    escalated = unconfirmed_keeps(bundle, [c for c in kept if c.intent == "keep"])
    escalated_set = set(escalated)
    kept_after = [c for c in kept if str(c.candidate_id) not in escalated_set]
    return KeptSpans(
        kept_spans_mezz=tuple(
            (int(c.source_span.start_frame), int(c.source_span.end_frame))
            for c in kept_after
        ),
        kept_candidate_ids=tuple(str(c.candidate_id) for c in kept_after),
        escalated_candidate_ids=escalated,
        escalated_spans_mezz=tuple(
            (int(c.source_span.start_frame), int(c.source_span.end_frame))
            for c in kept
            if str(c.candidate_id) in escalated_set
        ),
    )


__all__ = ["ESCALATE_BELOW", "KeptSpans", "derive_kept_spans", "unconfirmed_keeps"]
