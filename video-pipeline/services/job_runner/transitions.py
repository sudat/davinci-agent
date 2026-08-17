"""Transition table for the Job State Machine (PRD 6).

The main path is strictly linear; the only auxiliary edges are same-status
self transitions (retryable stage re-run, idempotent re-commit adopting a
new artifact hash), legal on every status except terminal ``FROZEN``.
Approval-gated edges (the operator checkpoints the PRD reserves for
humans: editorial approval and final approval) carry a required-approval
marker and a purpose; everything else — skips, backwards moves, and any
mutation out of ``FROZEN`` — is forbidden. ``PREVIEW_READY → RESOLVE_BUILT``
is called out as a gate bypass so the error names the missing approval
instead of a generic refusal.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Final

from services.contracts.primitives import Identifier, StrictModel
from services.job_runner.state_models import JobStatus  # noqa: TC001 (pydantic resolves it)

APPROVAL_PURPOSE_EDITORIAL: Final[Identifier] = "editorial_approval"
APPROVAL_PURPOSE_FINAL: Final[Identifier] = "final_approval"

MAIN_PATH: Final[tuple[JobStatus, ...]] = (
    "CREATED",
    "INGESTED",
    "NORMALIZED",
    "ANALYZED",
    "PLAN_PROPOSED",
    "PLAN_COMMITTED",
    "PREVIEW_READY",
    "EDITORIAL_APPROVED",
    "RESOLVE_BUILT",
    "QC_PASSED",
    "FINAL_APPROVED",
    "FROZEN",
)

_GATED_PURPOSES: Final[dict[tuple[JobStatus, JobStatus], Identifier]] = {
    ("PREVIEW_READY", "EDITORIAL_APPROVED"): APPROVAL_PURPOSE_EDITORIAL,
    ("QC_PASSED", "FINAL_APPROVED"): APPROVAL_PURPOSE_FINAL,
}


class TransitionEdge(StrictModel):
    """One legal edge; gated edges name the approval purpose they consume."""

    current_status: JobStatus
    target_status: JobStatus
    requires_approval: bool = False
    approval_purpose: Identifier | None = None


FORWARD_EDGES: Final[tuple[TransitionEdge, ...]] = tuple(
    TransitionEdge(
        current_status=current,
        target_status=target,
        requires_approval=(current, target) in _GATED_PURPOSES,
        approval_purpose=_GATED_PURPOSES.get((current, target)),
    )
    for current, target in pairwise(MAIN_PATH)
)

SELF_TRANSITION_STATUSES: Final[frozenset[JobStatus]] = frozenset(
    status for status in MAIN_PATH if status != "FROZEN"
)

GATE_BYPASS_EDGES: Final[frozenset[tuple[JobStatus, JobStatus]]] = frozenset(
    {("PREVIEW_READY", "RESOLVE_BUILT")}
)


def edge_for(current_status: JobStatus, target_status: JobStatus) -> TransitionEdge | None:
    """Return the legal edge for the pair, or ``None`` when forbidden."""

    if current_status == target_status:
        if current_status in SELF_TRANSITION_STATUSES:
            return TransitionEdge(
                current_status=current_status, target_status=target_status
            )
        return None
    for edge in FORWARD_EDGES:
        if edge.current_status == current_status and edge.target_status == target_status:
            return edge
    return None


def is_gate_bypass(current_status: JobStatus, target_status: JobStatus) -> bool:
    """True when the forbidden pair is a named approval-gate bypass."""

    return (current_status, target_status) in GATE_BYPASS_EDGES


__all__ = [
    "APPROVAL_PURPOSE_EDITORIAL",
    "APPROVAL_PURPOSE_FINAL",
    "FORWARD_EDGES",
    "GATE_BYPASS_EDGES",
    "MAIN_PATH",
    "SELF_TRANSITION_STATUSES",
    "TransitionEdge",
    "edge_for",
    "is_gate_bypass",
]
