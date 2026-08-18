"""Deterministic solver wiring for the Phase-1 fixture chain (Todo 42/43).

The planner input mirrors the frozen fixture spec (declared weights, budget,
ordering, must-include, declared observations) over the committed selection
plan's candidates; the committed EditPlan is generated with the declared A/V
audio bindings. Identical manifests and selections always produce identical
planner inputs, solutions, and plans.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from services.cli.planning import (
    EDIT_BASE,
    _segment_map,
    audio_bindings_for,
    edit_source_facts,
)
from services.editorial.candidate_models import ProposalProducer
from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.plan.constraint_planner import solve
from services.plan.edit_plan_generate import generate
from services.plan.edit_plan_models import EditPlan, SelectionPlanRef
from services.plan.planner_models import (
    CandidateObservation,
    PlannerInput,
    PlannerSolution,
    ScoreWeights,
)

if TYPE_CHECKING:
    from services.editorial.candidate_models import SelectionPlanProposal
    from services.editorial.reconcile import ReconciliationResult
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest

CHAIN_PRODUCER: Final = ProposalProducer(
    model_role_id="constraint-planner", contract_version="phase1-chain-v1"
)


def planner_input_for(
    manifest: Phase1TechnicalFixtureManifest,
    selection: SelectionPlanProposal,
    reconciled: ReconciliationResult,
) -> PlannerInput:
    mapping = _segment_map(reconciled)
    segments = {segment.segment_id: segment for segment in manifest.transcript.segments}
    observations = tuple(
        CandidateObservation(
            candidate_id=candidate.candidate_id,
            kind=segments[mapping[candidate.candidate_id]].kind,
            content_score=segments[mapping[candidate.candidate_id]].observed.content_score,
            clarity_score=segments[mapping[candidate.candidate_id]].observed.clarity_score,
        )
        for candidate in selection.candidates
        if mapping.get(candidate.candidate_id) in segments
    )
    declared = manifest.editorial_rules
    return PlannerInput(
        episode_id=selection.episode_id,
        candidates=selection.candidates,
        must_include=selection.must_include,
        budget=declared.duration_budget,
        ordering=selection.ordering,
        weights=ScoreWeights(
            content_weight=declared.scoring.content_weight,
            clarity_weight=declared.scoring.clarity_weight,
        ),
        observations=observations,
        order_locks=(),
        capability_allowlist=tuple(PHASE_0A_CAPABILITIES),
        edit_source=edit_source_facts(manifest),
    )


def selection_ref(
    manifest: Phase1TechnicalFixtureManifest,
    version: int,
    plan_sha256: str,
    artifact_id: str,
) -> SelectionPlanRef:
    return SelectionPlanRef(
        episode_id=manifest.fixture_id,
        plan_version=f"v{version}",
        plan_sha256=plan_sha256,
        plan_artifact_id=artifact_id,
    )


def committed_plan_for(
    manifest: Phase1TechnicalFixtureManifest,
    selection: SelectionPlanProposal,
    reconciled: ReconciliationResult,
    ref: SelectionPlanRef,
) -> EditPlan:
    solution = solve(planner_input_for(manifest, selection, reconciled))
    if not isinstance(solution, PlannerSolution):
        raise TypeError(f"planner refused the fixture plan: {solution}")
    return generate(
        selection,
        solution,
        plan_ref=ref,
        plan_base_version=EDIT_BASE,
        producer=CHAIN_PRODUCER,
        fixture_only=True,
        audio_bindings=audio_bindings_for(manifest, selection, reconciled),
    )


__all__ = [
    "CHAIN_PRODUCER",
    "committed_plan_for",
    "planner_input_for",
    "selection_ref",
]
