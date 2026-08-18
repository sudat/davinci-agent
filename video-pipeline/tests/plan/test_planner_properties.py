"""Todo 42 properties: deterministic solver invariants under generated inputs.

All properties hold for the DECLARED frozen rule (``drop-lowest-score-non-must``
under ``source-order-stable``):

* hard constraints: any feasible solution selects every must-include, stays
  within ``max_output_frames``, selects only keeps, and never exceeds the
  edit-source extent;
* budget exactness: allocated integer record frames form a contiguous
  partition of ``[0, total_duration_frames)`` and each record length equals
  the candidate's source span length;
* permutation stability: shuffling the input tuples never changes the
  canonical outcome bytes (feasible or infeasible);
* monotonicity (documented ACTUAL invariant — the frozen rule is NOT fully
  monotonic because a newly added higher-ranked keep can push a lower-ranked
  selected keep over budget): adding one keep candidate K never removes a
  previously selected candidate X that is a must-include or strictly outranks
  K in the drop order (weighted score, then lexicographically smaller id).
"""

from __future__ import annotations

import hashlib
import random

from hypothesis import assume, given
from hypothesis import strategies as st

from services.contracts.primitives import ArtifactRef
from services.editorial.candidate_models import (
    Candidate,
    CandidateProvenance,
    CandidateSourceRef,
    CandidateSpan,
    Handles,
    derive_candidate,
)
from services.fixtures.manifest_phase1 import DurationBudgetRules, OrderingRules
from services.foundation_io import canonical_model_bytes
from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.plan.constraint_planner import solve
from services.plan.planner_models import (
    CandidateObservation,
    DeclaredOrderLock,
    PlannerInput,
    PlannerSolution,
    ScoreWeights,
)
from services.validate.selection_models import EditSourceFacts
from services.validate.selection_schema import tuplize

ANALYZER_VERSION = "planner-prop-v1"
EDIT_SOURCE_SHA = hashlib.sha256(b"planner-prop:edit-source").hexdigest()
SOURCE_REF = CandidateSourceRef(source_id="planner-prop-source", edit_source_sha=EDIT_SOURCE_SHA)
EVIDENCE = (ArtifactRef(artifact_id="planner-prop-evidence", sha256=("e" * 64)),)
PROVENANCE = CandidateProvenance(analyzer_version=ANALYZER_VERSION, rule_ids=("p1-scoring-v1",))


def _span(start_frame: int, end_frame: int) -> CandidateSpan:
    return CandidateSpan(
        start_frame=start_frame,
        end_frame=end_frame,
        rate_num=30,
        rate_den=1,
        start_ms=start_frame * 1000 // 30,
        end_ms=end_frame * 1000 // 30,
    )


@st.composite
def planner_cases(
    draw: st.DrawFn,
    *,
    feasibility_bias: float = 0.5,
    guaranteed_feasible: bool = False,
    clean_context: bool = False,
) -> PlannerInput:
    extent = draw(st.integers(min_value=20, max_value=240))
    count = draw(st.integers(min_value=1, max_value=7))
    candidates: list[Candidate] = []
    observations: list[CandidateObservation] = []
    keeps: list[str] = []
    for _ in range(count):
        start = draw(st.integers(min_value=0, max_value=extent - 1))
        end = draw(st.integers(min_value=start + 1, max_value=extent))
        intent = draw(st.sampled_from(("keep", "remove")))
        candidate = derive_candidate(
            source_ref=SOURCE_REF,
            span=_span(start, end),
            intent=intent,
            analyzer_version=ANALYZER_VERSION,
            evidence=EVIDENCE,
            provenance=PROVENANCE,
            handles=Handles()
            if intent != "keep"
            else Handles(
                head_frames=draw(st.integers(min_value=0, max_value=5)),
                tail_frames=draw(st.integers(min_value=0, max_value=5)),
            ),
        )
        candidates.append(candidate)
        observations.append(
            CandidateObservation(
                candidate_id=candidate.candidate_id,
                kind=draw(st.sampled_from(("speech", "pause"))),
                content_score=draw(st.integers(min_value=0, max_value=10)),
                clarity_score=draw(st.integers(min_value=0, max_value=10)),
            )
        )
        if intent == "keep":
            keeps.append(candidate.candidate_id)
    assume(len({candidate.candidate_id for candidate in candidates}) == len(candidates))
    must = tuple(sorted(cid for cid in keeps if draw(st.booleans())))
    ids = [candidate.candidate_id for candidate in candidates]
    locks: list[DeclaredOrderLock] = []
    allowlist: tuple[str, ...] = PHASE_0A_CAPABILITIES
    if not clean_context:
        for _ in range(draw(st.integers(min_value=0, max_value=2))):
            before = draw(st.sampled_from(ids))
            after = draw(st.sampled_from(ids))
            if before == after:  # self-relations are invalid locks, not solver inputs
                continue
            locks.append(
                DeclaredOrderLock(
                    before_candidate_id=before,
                    after_candidate_id=after,
                    basis="planner-properties:generated-lock",
                )
            )
        allowlist = (
            PHASE_0A_CAPABILITIES
            if draw(st.booleans())
            else ("fixed_subtitle", "render", "media_intro_outro")
        )
    max_output_frames = (
        extent * max(count, 1)
        if guaranteed_feasible
        else draw(
            st.integers(min_value=1, max_value=max(2, int(extent * feasibility_bias * 4)))
        )
    )
    budget = DurationBudgetRules(
        rule_id="p1-budget-v1",
        max_output_frames=max_output_frames,
        min_output_frames=draw(st.integers(min_value=1, max_value=extent)),
        enforcement="drop-lowest-score-non-must",
    )
    return PlannerInput(
        episode_id="planner-prop-episode",
        candidates=tuple(candidates),
        must_include=must,
        budget=budget,
        ordering=OrderingRules(rule_id="p1-ordering-v1", rule="source-order-stable"),
        weights=ScoreWeights(
            content_weight=draw(st.integers(min_value=0, max_value=3)),
            clarity_weight=draw(st.integers(min_value=0, max_value=3)),
        ),
        observations=tuple(observations),
        order_locks=tuple(locks),
        capability_allowlist=allowlist,
        edit_source=EditSourceFacts(
            source_id=SOURCE_REF.source_id,
            edit_source_sha=EDIT_SOURCE_SHA,
            total_frames=extent,
        ),
    )


def _drop_rank(
    planner_input: PlannerInput, solution: PlannerSolution, candidate_id: str
) -> tuple[int, str]:
    """Rank in the drop order: lower score drops first; ties drop larger ids."""

    row = next(item for item in solution.scores if item.candidate_id == candidate_id)
    return (row.weighted_score, candidate_id)


@given(case=planner_cases())
def test_feasible_solutions_preserve_hard_constraints(case: PlannerInput) -> None:
    outcome = solve(case)
    if not isinstance(outcome, PlannerSolution):
        assume(False)  # noqa: FBT003 (hypothesis assume takes a positional condition)
        return
    by_id = {candidate.candidate_id: candidate for candidate in case.candidates}
    assert set(case.must_include) <= set(outcome.selected_candidate_ids)
    assert outcome.total_duration_frames <= case.budget.max_output_frames
    for candidate_id in outcome.selected_candidate_ids:
        candidate = by_id[candidate_id]
        assert candidate.intent == "keep"
        assert candidate.span.end_frame <= case.edit_source.total_frames


@given(case=planner_cases())
def test_allocated_frames_are_exact_and_contiguous(case: PlannerInput) -> None:
    outcome = solve(case)
    if not isinstance(outcome, PlannerSolution):
        assume(False)  # noqa: FBT003 (hypothesis assume takes a positional condition)
        return
    by_id = {candidate.candidate_id: candidate for candidate in case.candidates}
    cursor = 0
    total = 0
    for row in outcome.allocated:
        length = row.record_end_frame - row.record_start_frame
        assert row.record_start_frame == cursor
        assert length == by_id[row.candidate_id].duration_frames
        cursor = row.record_end_frame
        total += length
    assert total == outcome.total_duration_frames
    assert len(outcome.allocated) == len(outcome.selected_candidate_ids)
    assert len(outcome.scores) == len(outcome.selected_candidate_ids)


@given(case=planner_cases(), seed=st.integers(min_value=0, max_value=2**32 - 1))
def test_permuted_inputs_yield_identical_outcome_bytes(
    case: PlannerInput, seed: int
) -> None:
    base = solve(case)
    document = case.model_dump(mode="json")
    rng = random.Random(seed)  # noqa: S311 (deterministic permutation seed, not crypto)
    for key in ("candidates", "observations", "order_locks", "must_include"):
        value = document[key]
        assert isinstance(value, list)
        rng.shuffle(value)
    shuffled = PlannerInput.model_validate(tuplize(document))
    assert canonical_model_bytes(solve(shuffled)) == canonical_model_bytes(base)


@given(case=planner_cases(feasibility_bias=0.1), seed=st.integers(min_value=0, max_value=99))
def test_infeasible_inputs_yield_identical_report_bytes(case: PlannerInput, seed: int) -> None:
    base = solve(case)
    if isinstance(base, PlannerSolution):
        assume(False)  # noqa: FBT003 (hypothesis assume takes a positional condition)
        return
    document = case.model_dump(mode="json")
    rng = random.Random(seed)  # noqa: S311 (deterministic permutation seed, not crypto)
    for key in ("candidates", "observations", "order_locks", "must_include"):
        value = document[key]
        assert isinstance(value, list)
        rng.shuffle(value)
    shuffled = PlannerInput.model_validate(tuplize(document))
    assert canonical_model_bytes(solve(shuffled)) == canonical_model_bytes(base)


added_keep_spec = st.tuples(
    st.integers(min_value=0, max_value=239),  # start frame
    st.integers(min_value=1, max_value=60),  # span length
    st.integers(min_value=0, max_value=10),  # content score
    st.integers(min_value=0, max_value=10),  # clarity score
    st.integers(min_value=0, max_value=5),  # head handle
    st.integers(min_value=0, max_value=5),  # tail handle
)


@given(
    case=planner_cases(guaranteed_feasible=True, clean_context=True),
    added_spec=added_keep_spec,
)
def test_adding_a_keep_never_drops_strictly_higher_ranked_selections(
    case: PlannerInput, added_spec: tuple[int, int, int, int, int, int]
) -> None:
    start, length, content, clarity, head, tail = added_spec
    end = start + length
    assume(end <= case.edit_source.total_frames)
    base = solve(case)
    # guaranteed by construction (budget >= any keep subset total, no locks,
    # full allowlist); asserted so a regression in the strategy is visible.
    assert isinstance(base, PlannerSolution)
    added = derive_candidate(
        source_ref=SOURCE_REF,
        span=_span(start, end),
        intent="keep",
        analyzer_version=ANALYZER_VERSION,
        evidence=EVIDENCE,
        provenance=PROVENANCE,
        handles=Handles(head_frames=head, tail_frames=tail),
    )
    observation = CandidateObservation(
        candidate_id=added.candidate_id,
        kind="speech",
        content_score=content,
        clarity_score=clarity,
    )
    grown = case.model_copy(
        update={
            "candidates": (*case.candidates, added),
            "observations": (*case.observations, observation),
        }
    )
    assume(len({candidate.candidate_id for candidate in grown.candidates}) == len(grown.candidates))
    grown_outcome = solve(grown)
    # the added speech keep is itself droppable, so the grown case stays feasible
    assert isinstance(grown_outcome, PlannerSolution)

    added_score = case.weights.content_weight * content + case.weights.clarity_weight * clarity
    for candidate_id in base.selected_candidate_ids:
        if candidate_id in case.must_include:
            assert candidate_id in grown_outcome.selected_candidate_ids
            continue
        rank = _drop_rank(case, base, candidate_id)
        if rank[0] > added_score or (rank[0] == added_score and rank[1] < added.candidate_id):
            assert candidate_id in grown_outcome.selected_candidate_ids, (
                "adding a strictly lower-ranked keep never removes a higher-ranked "
                f"selection (frozen drop-lowest-score-non-must invariant): {candidate_id}"
            )
