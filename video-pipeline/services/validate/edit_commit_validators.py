"""The SEMANTIC / LOCK / CAPABILITY validators for Edit Plans (Todo 43).

SEMANTIC reconciles the plan against its base selection plan (recomputing
identities, links, ledger, and layout — never trusting plan claims) and
re-checks edit-source extent/binding plus episode consistency. LOCK defers
plans mutating a locked field of a locked span (walking adjust chains to
their parents). CAPABILITY maps every decision onto the frozen Phase-1
allowlist bound through the Phase-0A gate capabilities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from services.plan.edit_plan_reconcile import EditPlanReconcileError, reconcile
from services.validate.edit_commit_models import EditValidationRefusal, EditValidatorName

if TYPE_CHECKING:
    from services.editorial.candidate_models import Candidate, SelectionPlanProposal
    from services.plan.edit_plan_models import EditDecisionKind, EditPlan
    from services.validate.edit_commit_models import EditValidationContext
    from services.validate.selection_models import SelectionLock, SelectionLockField

EDIT_DECISION_REQUIRED_CAPABILITY: Final[dict[EditDecisionKind, str]] = {
    "keep": "base_cut",
    "adjust": "base_cut",
    "remove": "base_cut",
}
DECISION_LOCKED_FIELD: Final[dict[EditDecisionKind, SelectionLockField]] = {
    "remove": "selection",
    "adjust": "source_span",
}


def _refusal(
    validator: EditValidatorName, code: str, detail: str
) -> EditValidationRefusal:
    return EditValidationRefusal(validator=validator, code=code, detail=detail)


def validate_edit_semantic(
    plan: EditPlan,
    context: EditValidationContext,
    selection_plan: SelectionPlanProposal,
) -> EditValidationRefusal | None:
    """Recompute the whole plan against its frozen inputs."""

    try:
        reconcile(plan, selection_plan)
    except EditPlanReconcileError as error:
        return _refusal("semantic", error.code, error.detail)
    facts = context.edit_source
    for item in plan.items:
        if (
            item.source_ref.source_id != facts.source_id
            or item.source_ref.edit_source_sha != facts.edit_source_sha
        ):
            return _refusal(
                "semantic",
                "source_binding_mismatch",
                f"item {item.item_id} binds a different edit source than the context",
            )
        if item.span.end_frame > facts.total_frames or item.span.start_frame >= (
            facts.total_frames
        ):
            return _refusal(
                "semantic",
                "span_out_of_extent",
                f"item {item.item_id} span exceeds the edit source extent "
                f"[0,{facts.total_frames})",
            )
    episode = context.episode
    if plan.episode_id != episode.episode_id or plan.fixture_only != episode.fixture_only:
        return _refusal(
            "semantic",
            "episode_mismatch",
            "the plan episode/fixture_only contradicts the committed episode record",
        )
    return None


def _chain(candidate_id: str, by_id: dict[str, Candidate]) -> list[Candidate]:
    """The candidate plus every resolvable ancestor, stopping at cycles."""

    chain: list[Candidate] = []
    seen: set[str] = set()
    current = by_id.get(candidate_id)
    while current is not None and current.candidate_id not in seen:
        seen.add(current.candidate_id)
        chain.append(current)
        parent = current.parent_candidate_id
        current = None if parent is None else by_id.get(parent)
    return chain


def validate_edit_locks(
    plan: EditPlan,
    selection_plan: SelectionPlanProposal,
    locks: tuple[SelectionLock, ...],
) -> EditValidationRefusal | None:
    """A decision mutating a locked field of a locked span defers, never commits."""

    if not locks:
        return None
    by_id = {candidate.candidate_id: candidate for candidate in selection_plan.candidates}
    for row in plan.decisions:
        locked_field = DECISION_LOCKED_FIELD.get(row.kind)
        if locked_field is None:
            continue
        for member in _chain(row.candidate_id, by_id):
            for lock in locks:
                same_span = (
                    member.source_ref.source_id == lock.source_id
                    and member.source_ref.edit_source_sha == lock.edit_source_sha
                    and member.span == lock.span
                )
                if lock.field == locked_field and same_span:
                    return EditValidationRefusal(
                        validator="lock",
                        code="lock_conflict",
                        detail=(
                            f"decision {row.decision_id} mutates locked field "
                            f"{lock.field} of the span locked by {lock.grantor}"
                        ),
                        deferred=True,
                    )
    return None


def validate_edit_capabilities(
    plan: EditPlan, allowlist: tuple[str, ...]
) -> EditValidationRefusal | None:
    """Every decision must map onto the frozen Phase-1 capability allowlist."""

    allowed = set(allowlist)
    for row in plan.decisions:
        required = EDIT_DECISION_REQUIRED_CAPABILITY.get(row.kind)
        if required is None or required not in allowed:
            return _refusal(
                "capability",
                "capability_missing",
                f"decision {row.decision_id} of kind {row.kind} requires capability "
                f"{required!r} which is not in the frozen Phase-1 allowlist",
            )
    return None


__all__ = [
    "DECISION_LOCKED_FIELD",
    "EDIT_DECISION_REQUIRED_CAPABILITY",
    "validate_edit_capabilities",
    "validate_edit_locks",
    "validate_edit_semantic",
]
