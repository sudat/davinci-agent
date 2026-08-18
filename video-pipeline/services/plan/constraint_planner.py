"""The deterministic Duration and Constraint Planner (Todo 42).

``solve`` is PURE CODE over :class:`PlannerInput`: no model calls, no
clocks, no I/O. Hard constraints are verified independently (see
:mod:`services.plan.planner_checks`) in a fixed order; the first violation
returns the typed :class:`InfeasibilityReport` and NO plan. Infeasibility
is never silently forced: must-includes are never dropped and weights are
never edited.

Soft scoring follows the DECLARED frozen rule spec bound through the
manifest models: keeps enter in source order, and while the integer frame
total exceeds ``duration_budget.max_output_frames`` the enforcement rule
``drop-lowest-score-non-must`` drops the droppable (non-must, speech-kind)
keep with the minimal weighted score — ties drop the lexicographically
LARGEST candidate id first, the permutation-stable analogue of the frozen
rule's latest-position tie-break. The selected order is
``source-order-stable`` with lexicographic candidate-id tie-breaks, so the
solution is identical under any input permutation.

``min_output_frames`` is declared advisory under ``p1-budget-v1``: the
frozen enforcement governs overruns only, and no frozen fixture relies on
the minimum as a hard gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from services.plan.planner_checks import (
    candidate_order_key,
    check_hard_constraints,
    check_lock_positions,
)
from services.plan.planner_models import (
    AllocatedSpan,
    BudgetDropTrace,
    CandidateObservation,
    HandlePassThrough,
    InfeasibilityReport,
    OrderingTrace,
    PlannerInput,
    PlannerOutcome,
    PlannerSolution,
    ScoreWeights,
    SoftScoreBreakdown,
)

if TYPE_CHECKING:
    from services.editorial.candidate_models import Candidate

TIE_BREAK_RULE: Final[str] = "lexicographic-candidate-id"


def _enforce_budget(
    planner_input: PlannerInput,
    keeps: tuple[Candidate, ...],
    observations: dict[str, CandidateObservation],
) -> tuple[tuple[Candidate, ...], tuple[BudgetDropTrace, ...], InfeasibilityReport | None]:
    weights = planner_input.weights
    must = set(planner_input.must_include)
    max_frames = planner_input.budget.max_output_frames
    selected = list(keeps)
    total = sum(candidate.duration_frames for candidate in selected)
    dropped: list[BudgetDropTrace] = []

    while total > max_frames:
        droppable = [
            candidate
            for candidate in selected
            if candidate.candidate_id not in must
            and observations[candidate.candidate_id].kind == "speech"
        ]
        if not droppable:
            break
        scores = {
            candidate.candidate_id: observations[candidate.candidate_id].weighted_score(
                weights
            )
            for candidate in droppable
        }
        minimal = min(scores.values())
        victim = max(
            candidate_id for candidate_id, score in scores.items() if score == minimal
        )
        removed = next(
            candidate for candidate in droppable if candidate.candidate_id == victim
        )
        selected.remove(removed)
        total -= removed.duration_frames
        dropped.append(BudgetDropTrace(candidate_id=victim, weighted_score=minimal))

    if total > max_frames:
        return (), (), InfeasibilityReport(
            kind="max_duration_exceeded",
            constraint="duration_budget.max_output_frames",
            offending_candidate_ids=tuple(candidate.candidate_id for candidate in selected),
            explanation=(
                f"the remaining selected combination totals {total} frames and cannot "
                f"fit within max_output_frames={max_frames} after dropping every "
                "droppable non-must speech keep; must-include spans are never dropped"
            ),
            suggestions=(
                "increase duration_budget.max_output_frames",
                "reduce the must-include durations",
                "split the story block",
            ),
        )
    return tuple(selected), tuple(dropped), None


def _build_solution(
    planner_input: PlannerInput,
    selected: tuple[Candidate, ...],
    dropped: tuple[BudgetDropTrace, ...],
    observations: dict[str, CandidateObservation],
) -> PlannerSolution:
    weights: ScoreWeights = planner_input.weights
    allocated: list[AllocatedSpan] = []
    scores: list[SoftScoreBreakdown] = []
    handles: list[HandlePassThrough] = []
    cursor = 0
    for candidate in selected:
        observation = observations[candidate.candidate_id]
        allocated.append(
            AllocatedSpan(
                candidate_id=candidate.candidate_id,
                source_span=candidate.span,
                record_start_frame=cursor,
                record_end_frame=cursor + candidate.duration_frames,
            )
        )
        scores.append(
            SoftScoreBreakdown(
                candidate_id=candidate.candidate_id,
                kind=observation.kind,
                content_score=observation.content_score,
                clarity_score=observation.clarity_score,
                content_weight=weights.content_weight,
                clarity_weight=weights.clarity_weight,
                weighted_score=observation.weighted_score(weights),
            )
        )
        handles.append(
            HandlePassThrough(
                candidate_id=candidate.candidate_id,
                head_frames=candidate.handles.head_frames,
                tail_frames=candidate.handles.tail_frames,
            )
        )
        cursor += candidate.duration_frames
    return PlannerSolution(
        episode_id=planner_input.episode_id,
        ordering_rule_id=planner_input.ordering.rule_id,
        selected_candidate_ids=tuple(c.candidate_id for c in selected),
        allocated=tuple(allocated),
        total_duration_frames=cursor,
        scores=tuple(scores),
        ordering_trace=OrderingTrace(
            rule_id=planner_input.ordering.rule_id,
            rule=planner_input.ordering.rule,
            tie_break=TIE_BREAK_RULE,  # type: ignore[arg-type] (frozen literal)
            handles=tuple(handles),
            budget_dropped=dropped,
        ),
    )


def solve(planner_input: PlannerInput) -> PlannerOutcome:
    """Deterministically solve the declared constraints or report infeasibility."""

    ordered = tuple(sorted(planner_input.candidates, key=candidate_order_key))
    observations = {
        observation.candidate_id: observation for observation in planner_input.observations
    }

    refusal = check_hard_constraints(planner_input, ordered)
    if refusal is not None:
        return refusal

    keeps = tuple(candidate for candidate in ordered if candidate.intent == "keep")
    selected, dropped, over_budget = _enforce_budget(planner_input, keeps, observations)
    if over_budget is not None:
        return over_budget

    conflict = check_lock_positions(planner_input, selected)
    if conflict is not None:
        return conflict

    return _build_solution(planner_input, selected, dropped, observations)


__all__ = ["solve"]
