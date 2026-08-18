"""The Todo-41/43 commit authorities wired for the real-episode chain (Todo 46).

The authorities run unchanged over the real proposal/plans; the only real-path
additions are the job-state PLAN_PROPOSED/PLAN_COMMITTED transitions around
each serialized commit and the typed refusal mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.gates.phase0a import PHASE_0A_CAPABILITIES
from services.job_runner.cas import apply_transition, current_job_state
from services.plan.constraint_planner import solve
from services.plan.edit_plan_generate import generate
from services.plan.edit_plan_models import EditPlan, SelectionPlanRef
from services.plan.planner_models import PlannerSolution
from services.validate.edit_commit import EditCommitAuthority
from services.validate.edit_commit_models import (
    EditCommitOutcome,
    EditCommitRefusal,
    EditValidationContext,
)
from services.validate.edit_commit_store import initialize_edit_plan_store
from services.validate.selection_commit import SelectionCommitAuthority
from services.validate.selection_models import (
    CommittedEpisodeRecord,
    EditSourceFacts,
    SelectionCommitOutcome,
    SelectionCommitRefusal,
    ValidationContext,
)
from services.validate.selection_plan_store import initialize_plan_store

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore
    from services.editorial.candidate_models import CandidatePool, SelectionPlanProposal
    from services.editorial.reconcile import ReconciliationResult
    from services.job_runner.state_store import StateStore

from services.cli.real_plan import (
    EDIT_BASE,
    REAL_PRODUCER,
    SELECTION_BASE,
    RealPlanError,
    planner_input,
)

JOB_ID: Final = "job-real-episode-run"


def advance(state: StateStore, target: str, artifact: str) -> None:
    snapshot = current_job_state(state, JOB_ID)
    apply_transition(
        state,
        JOB_ID,
        expected_status=snapshot.status,
        expected_parent_hash=snapshot.adopted_artifact_hash,
        new_status=target,  # type: ignore[arg-type] (stage names are statuses)
        new_artifact_hash=artifact,
    )


def commit_selection(  # noqa: PLR0913 (authority wiring is the commit contract)
    *,
    state: StateStore,
    artifacts: ArtifactStore,
    registry: ArtifactRegistry,
    out_dir: Path,
    selection: SelectionPlanProposal,
    episode: CommittedEpisodeRecord,
    facts: EditSourceFacts,
    request_hash: str,
) -> SelectionCommitOutcome:
    selection_dir = out_dir / "selection-plan"
    initialize_plan_store(
        selection_dir, episode_id=episode.episode_id, base_version=SELECTION_BASE
    )
    advance(state, "PLAN_PROPOSED", request_hash)
    outcome = SelectionCommitAuthority(
        artifact_store=artifacts,
        registry=registry,
        state_store=state,
        job_id=JOB_ID,
        plan_dir=selection_dir,
        context=ValidationContext(
            episode=episode,
            edit_source=facts,
            capability_allowlist=tuple(PHASE_0A_CAPABILITIES),
            locks=(),
        ),
        holder="real-chain",
    ).commit(selection, now=100, ttl_seconds=100)
    if isinstance(outcome, SelectionCommitRefusal):
        raise RealPlanError("selection_refused", f"{outcome.code}: {outcome.detail}")
    advance(state, "PLAN_COMMITTED", outcome.plan_sha256)
    return outcome


def commit_edit_plan(  # noqa: PLR0913 (authority wiring is the commit contract)
    *,
    state: StateStore,
    artifacts: ArtifactStore,
    registry: ArtifactRegistry,
    out_dir: Path,
    selection: SelectionPlanProposal,
    pool: CandidatePool,
    reconciled: ReconciliationResult,
    facts: EditSourceFacts,
    episode: CommittedEpisodeRecord,
    selection_outcome: SelectionCommitOutcome,
) -> tuple[EditCommitOutcome, EditPlan]:
    solution = solve(planner_input(selection, pool, reconciled, facts))
    if not isinstance(solution, PlannerSolution):
        raise RealPlanError(
            "planner_infeasible", f"{solution.kind}: {solution.explanation}"
        )
    ref = SelectionPlanRef(
        episode_id=selection.episode_id,
        plan_version=f"v{selection_outcome.version}",
        plan_sha256=selection_outcome.plan_sha256,
        plan_artifact_id=selection_outcome.plan_artifact_id,
    )
    plan = generate(
        selection,
        solution,
        plan_ref=ref,
        plan_base_version=EDIT_BASE,
        producer=REAL_PRODUCER,
        fixture_only=False,
        audio_bindings=(),
    )
    edit_dir = out_dir / "edit-plan"
    initialize_edit_plan_store(
        edit_dir, episode_id=selection.episode_id, base_version=EDIT_BASE
    )
    outcome = EditCommitAuthority(
        artifact_store=artifacts,
        registry=registry,
        state_store=state,
        job_id=JOB_ID,
        edit_plan_dir=edit_dir,
        context=EditValidationContext(
            episode=episode,
            edit_source=facts,
            capability_allowlist=tuple(PHASE_0A_CAPABILITIES),
            selection_base=ref,
            locks=(),
        ),
        selection_plan=selection,
        holder="real-chain",
    ).commit(plan, now=100, ttl_seconds=100)
    if isinstance(outcome, EditCommitRefusal):
        raise RealPlanError("edit_plan_refused", f"{outcome.code}: {outcome.detail}")
    return outcome, plan



__all__ = ["advance", "commit_edit_plan", "commit_selection"]
