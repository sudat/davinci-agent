"""Todo 42 acceptance: the deterministic Duration and Constraint Planner.

The solver is PURE CODE over the committed selection plan: hard constraints
(invalid source, capability, must-include presence, order locks, duration
budget) are verified independently and reported through TYPED infeasibility
reports — never silently forced (no must-include drops, no weight tampering);
soft scoring allocates exact integer frames under the frozen
``drop-lowest-score-non-must`` rule with lexicographic candidate-id
tie-breaks that are stable under input permutation; model-authored
``solver_override`` fields are typed-rejected before any solve.
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import TYPE_CHECKING

import pytest

from services.foundation_io import canonical_model_bytes
from services.plan.constraint_planner import solve
from services.plan.planner_models import InfeasibilityReport, PlannerSolution
from services.plan.planner_parse import PlannerInputError, parse_planner_input
from tests.plan.support import (
    ALL_FIXTURE_IDS,
    candidate_id_of_segment,
    golden_selection,
    order_lock,
    planner_input_for,
    segment_map,
)

if TYPE_CHECKING:
    from services.plan.planner_models import PlannerInput

REF01 = "p1-ref-01-clean-ja"
REF02 = "p1-ref-02-pauses-fillers"
REF03 = "p1-ref-03-multi-take-must-include"
GHOST_ID = hashlib.sha256(b"ghost:before").hexdigest()


def _json_object(payload: str | bytes) -> dict[str, object]:
    document: object = json.loads(payload)
    assert isinstance(document, dict)
    return document


def _solved(planner_input: PlannerInput) -> PlannerSolution:
    outcome = solve(planner_input)
    assert isinstance(outcome, PlannerSolution)
    return outcome


def _infeasible(planner_input: PlannerInput) -> InfeasibilityReport:
    outcome = solve(planner_input)
    assert isinstance(outcome, InfeasibilityReport)
    return outcome


def _selected_segments(fixture_id: str, solution: PlannerSolution) -> list[str]:
    mapping = segment_map(fixture_id)
    return [mapping[candidate_id] for candidate_id in solution.selected_candidate_ids]


# ---------------------------------------------------------------- happy paths


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_golden_fixture_selection_is_stable(fixture_id: str) -> None:
    solution = _solved(planner_input_for(fixture_id))
    golden = golden_selection(fixture_id)

    assert _selected_segments(fixture_id, solution) == golden["selected_ids"]
    assert solution.total_duration_frames == golden["total_selected_frames"]

    mapping = segment_map(fixture_id)
    dropped = [mapping[trace.candidate_id] for trace in solution.ordering_trace.budget_dropped]
    assert dropped == golden["budget_dropped"]


def test_p1_ref_03_drops_m7_and_keeps_must_include_per_golden() -> None:
    solution = _solved(planner_input_for(REF03))
    assert _selected_segments(REF03, solution) == ["m0", "t1c", "t2a", "t2b"]
    assert solution.total_duration_frames == 600

    t2a = candidate_id_of_segment(REF03, "t2a")
    assert t2a in solution.selected_candidate_ids, "must-include is never dropped"
    m7 = candidate_id_of_segment(REF03, "m7")
    assert [trace.candidate_id for trace in solution.ordering_trace.budget_dropped] == [m7]
    assert solution.ordering_trace.budget_dropped[0].weighted_score == 24


def test_solution_allocates_exact_integer_record_frames() -> None:
    planner_input = planner_input_for(REF02)
    solution = _solved(planner_input)
    cursor = 0
    recomputed = 0
    assert len(solution.allocated) == len(solution.selected_candidate_ids)
    for row in solution.allocated:
        length = row.record_end_frame - row.record_start_frame
        assert row.record_start_frame == cursor, "record spans are contiguous from zero"
        assert length == row.source_span.end_frame - row.source_span.start_frame
        cursor += length
        recomputed += length
    # misleading-success guard: totals are recomputed from raw spans, never trusted
    assert recomputed == solution.total_duration_frames == 614
    assert solution.total_duration_frames <= planner_input.budget.max_output_frames


def test_soft_score_breakdown_matches_declared_weights() -> None:
    solution = _solved(planner_input_for(REF01))
    assert solution.scores
    for row in solution.scores:
        assert row.content_weight == 2
        assert row.clarity_weight == 1
        assert row.weighted_score == 2 * row.content_score + row.clarity_score
    first = solution.scores[0]
    assert (first.content_score, first.clarity_score, first.weighted_score) == (9, 9, 27)


def test_handles_and_rule_pass_through_into_ordering_trace() -> None:
    solution = _solved(planner_input_for(REF01))
    trace = solution.ordering_trace
    assert trace.rule_id == "p1-ordering-v1"
    assert trace.rule == "source-order-stable"
    assert trace.tie_break == "lexicographic-candidate-id"
    assert [row.candidate_id for row in trace.handles] == list(solution.selected_candidate_ids)
    for row in trace.handles:  # Phase-1 fixtures declare no handles
        assert (row.head_frames, row.tail_frames) == (0, 0)


def test_solve_is_repeatable_and_never_mutates_input() -> None:
    first_input = planner_input_for(REF03)
    before = canonical_model_bytes(first_input)
    first = solve(first_input)
    second = solve(planner_input_for(REF03))
    assert canonical_model_bytes(first) == canonical_model_bytes(second)
    assert canonical_model_bytes(first_input) == before, "the solver never edits its input"


# ------------------------------------------------------------------- failures


def test_must_include_over_budget_is_infeasible_without_plan() -> None:
    report = _infeasible(planner_input_for(REF03, max_output_frames=100))
    assert report.kind == "max_duration_exceeded"
    assert report.constraint == "duration_budget.max_output_frames"
    t2a = candidate_id_of_segment(REF03, "t2a")
    assert t2a in report.offending_candidate_ids, "the must-include combination is named"
    assert "100" in report.explanation
    assert "plan" not in report.model_dump(), "infeasible outcomes emit NO plan"
    assert not isinstance(report, PlannerSolution)


def test_ghost_must_include_reports_typed_conflict() -> None:
    report = _infeasible(planner_input_for(REF01, must_include=("f" * 64,)))
    assert report.kind == "must_include_conflict"
    assert report.constraint == "must_include"
    assert report.offending_candidate_ids == ("f" * 64,)


def test_lock_order_conflict_is_infeasible_without_plan() -> None:
    lock = order_lock("t2b", "m0", REF03)  # source order places m0 before t2b
    report = _infeasible(planner_input_for(REF03, order_locks=(lock,)))
    assert report.kind == "lock_order_conflict"
    assert report.constraint == "order_lock"
    assert set(report.offending_candidate_ids) == {
        candidate_id_of_segment(REF03, "m0"),
        candidate_id_of_segment(REF03, "t2b"),
    }
    assert "plan" not in report.model_dump()


def test_unresolved_order_lock_endpoint_reports_conflict() -> None:
    lock = order_lock("s1", "s2", REF01)
    broken = lock.model_copy(update={"before_candidate_id": GHOST_ID})
    report = _infeasible(planner_input_for(REF01, order_locks=(broken,)))
    assert report.kind == "lock_order_conflict"
    assert GHOST_ID in report.offending_candidate_ids


def test_missing_capability_is_infeasible_without_plan() -> None:
    report = _infeasible(planner_input_for(REF01, allowlist=("fixed_subtitle", "render")))
    assert report.kind == "capability_unavailable"
    assert report.constraint == "capability_allowlist"
    expected = {candidate.candidate_id for candidate in planner_input_for(REF01).candidates}
    assert set(report.offending_candidate_ids) == expected
    assert "plan" not in report.model_dump()


def test_invalid_source_span_is_infeasible_without_plan() -> None:
    report = _infeasible(planner_input_for(REF01, edit_total_frames=200))
    assert report.kind == "invalid_source"
    assert report.constraint == "edit_source"
    expected = {
        candidate_id_of_segment(REF01, segment_id)
        for segment_id in ("s2", "s3", "s4")  # s1 [0,150) still fits
    }
    assert set(report.offending_candidate_ids) == expected


def test_source_identity_mismatch_is_invalid_source() -> None:
    report = _infeasible(planner_input_for(REF01, edit_source_sha="a" * 64))
    assert report.kind == "invalid_source"
    assert len(report.offending_candidate_ids) == len(planner_input_for(REF01).candidates)


def test_infeasible_report_bytes_stable_under_permutation() -> None:
    base = _infeasible(planner_input_for(REF03, max_output_frames=100))
    document = _json_object(planner_input_for(REF03, max_output_frames=100).model_dump_json())
    rng = random.Random(42)  # noqa: S311 (deterministic permutation seed, not crypto)
    candidates = document["candidates"]
    assert isinstance(candidates, list)
    rng.shuffle(candidates)
    observations = document["observations"]
    assert isinstance(observations, list)
    rng.shuffle(observations)
    outcome = solve(parse_planner_input(json.dumps(document)))
    assert canonical_model_bytes(outcome) == canonical_model_bytes(base)


# ------------------------------------------------------- model/solver boundary


def test_solver_override_fields_are_rejected() -> None:
    base = _json_object(planner_input_for(REF01).model_dump_json())
    weights = _json_object(json.dumps(base["weights"]))
    candidates = base["candidates"]
    assert isinstance(candidates, list)
    first = candidates[0]
    assert isinstance(first, dict)
    first_poisoned = {**first, "force_drop": True}
    poisoned_documents = (
        {**base, "solver_override": {"selected": True}},
        {**base, "weights": {**weights, "weight_override": 5}},
        {**base, "solution": {"selected_candidate_ids": []}},
        {**base, "candidates": [first_poisoned, *candidates[1:]]},
    )
    for document in poisoned_documents:
        with pytest.raises(PlannerInputError) as failure:
            parse_planner_input(document)
        assert failure.value.code == "solver_override"


def test_float_durations_are_rejected() -> None:
    base = _json_object(planner_input_for(REF01).model_dump_json())
    weights = _json_object(json.dumps(base["weights"]))
    budget = _json_object(json.dumps(base["budget"]))
    float_documents = (
        {**base, "weights": {**weights, "content_weight": 1.5}},
        {**base, "budget": {**budget, "max_output_frames": 600.5}},
    )
    for document in float_documents:
        with pytest.raises(PlannerInputError) as failure:
            parse_planner_input(document)
        assert failure.value.code == "schema"


def test_parse_returns_strict_planner_input() -> None:
    parsed = parse_planner_input(planner_input_for(REF01).model_dump_json())
    assert canonical_model_bytes(parsed) == canonical_model_bytes(planner_input_for(REF01))
