"""Selection/edit commit tail for the real-episode chain (Todo 46).

The Todo-41 and Todo-43 commit authorities run unchanged over the real
proposal/plans; the Todo-42 planner consumes the committed selection with
deterministic neutral observations (the real analyzer contract carries no
per-segment model scores). The deterministic solver and the Todo-44
production compiler are reused exactly as the fixture chain uses them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from services.compile.conform_inputs import EditSourceGeometry
from services.compile.production_compiler import compile_production
from services.contracts.primitives import RationalFrameRate
from services.editorial.candidate_models import (
    CandidatePool,
    ProposalProducer,
    SelectionPlanProposal,
)
from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.plan.planner_models import (
    CandidateObservation,
    PlannerInput,
    ScoreWeights,
)

if TYPE_CHECKING:
    from services.compile.production_compiler import CompileProductionResult
    from services.compile.subtitle_policy import SubtitleQcPolicy, TranscriptCueSource
    from services.editorial.candidate_models import SegmentKind
    from services.editorial.reconcile import ReconciliationResult
    from services.plan.edit_plan_models import EditPlan
    from services.validate.edit_commit_models import EditCommitOutcome
    from services.validate.selection_models import (
        EditSourceFacts,
        SelectionCommitOutcome,
    )

EDIT_BASE: Final = "edit-base-v0"
SELECTION_BASE: Final = "plan-base-v0"
NEUTRAL_SCORE: Final = 5
WEIGHTS: Final = ScoreWeights(content_weight=2, clarity_weight=1)
REAL_PRODUCER: Final = ProposalProducer(
    model_role_id="constraint-planner", contract_version="real-chain-v1"
)


class RealPlanError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RealPlanOutcome:
    selection: SelectionCommitOutcome
    selection_proposal: SelectionPlanProposal
    reconciled: ReconciliationResult
    edit: EditCommitOutcome
    plan: EditPlan
    production: CompileProductionResult


def planner_input(
    selection: SelectionPlanProposal,
    pool: CandidatePool,
    reconciled: ReconciliationResult,
    facts: EditSourceFacts,
) -> PlannerInput:
    segment_of = {
        candidate_id: link.segment_id
        for link in reconciled.segment_links
        for candidate_id in link.candidate_ids
    }
    kind_of: dict[str, SegmentKind] = {
        record.segment_id: record.kind for record in pool.segments
    }
    observations = tuple(
        CandidateObservation(
            candidate_id=candidate.candidate_id,
            kind=kind_of.get(segment_of.get(candidate.candidate_id, ""), "speech"),
            content_score=NEUTRAL_SCORE,
            clarity_score=NEUTRAL_SCORE,
        )
        for candidate in selection.candidates
        if candidate.intent == "keep"
    )
    return PlannerInput(
        episode_id=selection.episode_id,
        candidates=selection.candidates,
        must_include=selection.must_include,
        budget=selection.budget,
        ordering=selection.ordering,
        weights=WEIGHTS,
        observations=observations,
        order_locks=(),
        capability_allowlist=tuple(PHASE_0A_CAPABILITIES),
        edit_source=facts,
    )


def compile_ir(  # noqa: PLR0913 (compile wiring over the committed plan)
    plan: EditPlan,
    *,
    source_id: str,
    total_frames: int,
    cue_source: TranscriptCueSource,
    policy: SubtitleQcPolicy,
    episode_id: str,
) -> CompileProductionResult:
    geometry = EditSourceGeometry(
        source_id=source_id,
        frame_rate=RationalFrameRate(num=30, den=1),
        total_frames=total_frames,
        audio_sample_rate=48000,
    )
    return compile_production(
        plan, geometry, cue_source, policy, artifact_id=f"timeline-ir-{episode_id}",
        merge_placements=True,
    )


__all__ = [
    "EDIT_BASE",
    "REAL_PRODUCER",
    "SELECTION_BASE",
    "RealPlanError",
    "RealPlanOutcome",
    "compile_ir",
    "planner_input",
]
