"""Hard-constraint checks for the deterministic planner (Todo 42).

Each check recomputes one hard constraint from the RAW planner input —
never trusting upstream validation — and returns the typed
:class:`InfeasibilityReport` for the first violation in the fixed check
order (invalid source → capability → must-include presence → order-lock
endpoints). Offending ids are always derived from canonically sorted
structures so reports are byte-stable under input permutation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.plan.planner_models import DeclaredOrderLock, InfeasibilityReport, PlannerInput
from services.validate.selection_validators import INTENT_REQUIRED_CAPABILITY

if TYPE_CHECKING:
    from services.editorial.candidate_models import Candidate
    from services.validate.selection_models import EditSourceFacts


def _order_key(candidate: Candidate) -> tuple[int, int, str]:
    return (candidate.span.start_frame, candidate.span.end_frame, candidate.candidate_id)


def _lock_key(lock: DeclaredOrderLock) -> tuple[str, str]:
    return (lock.before_candidate_id, lock.after_candidate_id)


def _report(
    kind: str,
    constraint: str,
    offending: tuple[str, ...],
    explanation: str,
    suggestions: tuple[str, ...],
) -> InfeasibilityReport:
    return InfeasibilityReport(
        kind=kind,  # type: ignore[arg-type] (callers pass literal kinds only)
        constraint=constraint,
        offending_candidate_ids=offending,
        explanation=explanation,
        suggestions=suggestions,
    )


def _source_invalid(candidate: Candidate, edit_source: EditSourceFacts) -> bool:
    return (
        candidate.source_ref.source_id != edit_source.source_id
        or candidate.source_ref.edit_source_sha != edit_source.edit_source_sha
        or candidate.span.end_frame > edit_source.total_frames
    )


def _check_source(
    planner_input: PlannerInput, ordered: tuple[Candidate, ...]
) -> InfeasibilityReport | None:
    offenders = tuple(
        candidate.candidate_id
        for candidate in ordered
        if _source_invalid(candidate, planner_input.edit_source)
    )
    if offenders:
        return _report(
            "invalid_source",
            "edit_source",
            offenders,
            f"{len(offenders)} candidate spans exceed the edit source extent "
            f"[0,{planner_input.edit_source.total_frames}) or bind a different "
            "edit source identity",
            ("re-ingest the edit source and rebuild the candidate pool",),
        )
    return None


def _check_capability(
    planner_input: PlannerInput, ordered: tuple[Candidate, ...]
) -> InfeasibilityReport | None:
    allowed = set(planner_input.capability_allowlist)
    offenders = tuple(
        candidate.candidate_id
        for candidate in ordered
        if INTENT_REQUIRED_CAPABILITY[candidate.intent] not in allowed
    )
    if offenders:
        return _report(
            "capability_unavailable",
            "capability_allowlist",
            offenders,
            f"{len(offenders)} candidates require intents whose capability is not "
            f"in the declared allowlist {sorted(allowed)}",
            ("extend the capability allowlist through a new gate version",),
        )
    return None


def _check_must_include(
    planner_input: PlannerInput, by_id: dict[str, Candidate]
) -> InfeasibilityReport | None:
    missing = tuple(
        sorted(
            candidate_id
            for candidate_id in set(planner_input.must_include)
            if candidate_id not in by_id or by_id[candidate_id].intent != "keep"
        )
    )
    if missing:
        return _report(
            "must_include_conflict",
            "must_include",
            missing,
            f"{len(missing)} must-include ids are absent from the keep candidates; "
            "the solver never drops, rewrites, or invents must-includes",
            ("reconcile the must-include set with the committed candidates",),
        )
    return None


def _check_lock_endpoints(
    planner_input: PlannerInput, by_id: dict[str, Candidate]
) -> InfeasibilityReport | None:
    for lock in sorted(planner_input.order_locks, key=_lock_key):
        unresolved = sorted(
            candidate_id
            for candidate_id in (lock.before_candidate_id, lock.after_candidate_id)
            if candidate_id not in by_id
        )
        if unresolved:
            return _report(
                "lock_order_conflict",
                "order_lock",
                tuple(unresolved),
                f"order lock {lock.before_candidate_id} -> {lock.after_candidate_id} "
                f"({lock.basis}) references candidates that do not exist",
                ("re-issue the order lock against committed candidate ids",),
            )
    return None


def check_hard_constraints(
    planner_input: PlannerInput, ordered: tuple[Candidate, ...]
) -> InfeasibilityReport | None:
    """Fixed-order hard checks; the first violation wins deterministically."""

    by_id = {candidate.candidate_id: candidate for candidate in ordered}
    for refusal in (
        _check_source(planner_input, ordered),
        _check_capability(planner_input, ordered),
        _check_must_include(planner_input, by_id),
        _check_lock_endpoints(planner_input, by_id),
    ):
        if refusal is not None:
            return refusal
    return None


def check_lock_positions(
    planner_input: PlannerInput, selected: tuple[Candidate, ...]
) -> InfeasibilityReport | None:
    """Declared order locks must hold on the final selected order."""

    position = {
        candidate.candidate_id: index for index, candidate in enumerate(selected)
    }
    for lock in sorted(planner_input.order_locks, key=_lock_key):
        before = position.get(lock.before_candidate_id)
        after = position.get(lock.after_candidate_id)
        if before is None or after is None or before < after:
            continue
        return _report(
            "lock_order_conflict",
            "order_lock",
            tuple(sorted((lock.before_candidate_id, lock.after_candidate_id))),
            f"order lock {lock.before_candidate_id} -> {lock.after_candidate_id} "
            f"({lock.basis}) contradicts the declared "
            f"{planner_input.ordering.rule} ordering",
            ("unlock the conflicting order relation",),
        )
    return None


def candidate_order_key(candidate: Candidate) -> tuple[int, int, str]:
    """The canonical source-order-stable ordering key (public to the solver)."""

    return _order_key(candidate)


__all__ = ["candidate_order_key", "check_hard_constraints", "check_lock_positions"]
