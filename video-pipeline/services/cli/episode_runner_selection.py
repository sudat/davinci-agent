"""Selection re-entry for adopted consultation policies (slice 2).

``stage_selection`` (in ``episode_runner_rebuild``) calls this module's two
seams: :func:`rerun_director_with_policy` reloads the persisted chain inputs
(``run/selection-inputs.json`` — analyzers never re-run) and re-runs the SAME
``select_and_reconcile`` path the initial chain uses, with the adopted policy
as constraint input; :func:`derive_policy_plan` turns the rerun into a
review-plane plan through the SAME pure functions the chain uses
(planner feasibility, edit-plan generate, production compile, review-plane
projection). Committing is ``review_command.policy_commit`` (single writer:
the rebuild runner holds the episode lock). Every failure is a typed error
and commits nothing — the outcome journal then shows the failure and the
consultation UI stays available (相談へ戻る).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from services.cli.compile_ir import qc_policy
from services.cli.project import ProjectionError, ReviewSubtitle, project_plan
from services.cli.real_director import select_and_reconcile
from services.cli.real_plan import EDIT_BASE, REAL_PRODUCER, compile_ir, planner_input
from services.cli.real_pool import SpeechSegment, cue_source_for
from services.cli.real_selection_inputs import (
    SelectionInputsError,
    SelectionInputsV1,
    load_selection_inputs,
)
from services.compile.production_errors import CompileProductionError
from services.contracts.primitives import RationalFrameRate
from services.episode_cockpit.consultation_store import AdoptedPolicyV1, policy_summary
from services.episode_cockpit.policy_settings import (
    derive_policy_settings,
    select_policy_subtitles,
)
from services.plan.constraint_planner import solve
from services.plan.edit_plan_generate import EditPlanGenerationError, generate
from services.plan.edit_plan_models import SelectionPlanRef
from services.plan.planner_models import PlannerSolution
from services.validate.selection_models import EditSourceFacts
from services.validate.selection_plan_store import (
    SelectionPlanStoreError,
    load_index,
)

if TYPE_CHECKING:
    from services.cli.real_analyze import RealAnalysis
    from services.cli.real_director import DirectorOutcome
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.editorial.candidate_models import CandidatePool, SelectionPlanProposal
    from services.editorial.reconcile import ReconciliationResult

RUN_DIR_NAME = "run"
SELECTION_PLAN_DIR_NAME = "selection-plan"


class SelectionRerunError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class PolicyDerivationError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SelectionRerun:
    outcome: DirectorOutcome
    selection: SelectionPlanProposal
    reconciled: ReconciliationResult
    pool: CandidatePool
    speech: tuple[SpeechSegment, ...]


def rebuild_analysis(inputs: SelectionInputsV1) -> RealAnalysis:
    """Reconstruct the seam's analysis argument from persisted inputs."""

    from services.cli.real_analyze import RealAnalysis  # noqa: PLC0415 (dataclass only)

    return RealAnalysis(
        record=inputs.record,
        transcript=inputs.transcript,
        dialogue=inputs.dialogue,
        visual=inputs.visual,
        evidence=inputs.evidence,
    )


def rerun_director_with_policy(
    episode_root: Path,
    policy: AdoptedPolicyV1,
    env: dict[str, str],
    runtime_path: Path | None = None,
) -> SelectionRerun:
    """Re-run the initial chain's director seam with the adopted policy."""

    from services.cli.real_director import (  # noqa: PLC0415 (typed error only)
        RealDirectorError,
    )

    run_dir = episode_root / RUN_DIR_NAME
    try:
        inputs = load_selection_inputs(run_dir)
    except SelectionInputsError as error:
        raise SelectionRerunError(error.code, error.detail) from error
    speech = tuple(
        SpeechSegment(
            segment_id=segment.segment_id,
            text=segment.text,
            start_frame=segment.start_frame,
            end_frame=segment.end_frame,
        )
        for segment in inputs.speech
    )
    try:
        outcome, selection, reconciled, _policy_file = select_and_reconcile(
            episode_id=inputs.episode_id,
            source_id=inputs.source_id,
            total_frames=inputs.total_frames,
            analysis=rebuild_analysis(inputs),
            pool=inputs.pool,
            speech=speech,
            policy_path=None,
            out_dir=run_dir,
            env=env,
            adopted_policy=policy_summary(policy),
            runtime_path=runtime_path,
        )
    except RealDirectorError as error:
        raise SelectionRerunError(error.code, error.detail) from error
    return SelectionRerun(
        outcome=outcome,
        selection=selection,
        reconciled=reconciled,
        pool=inputs.pool,
        speech=speech,
    )


def selection_base_ref(episode_root: Path, episode_id: str) -> SelectionPlanRef:
    """The latest COMMITTED selection identity the derived plan records.

    The policy rerun re-decides within the committed base (same candidate
    universe, same base label) but is never committed to the selection
    store itself — inventing a version there would fabricate lineage, so
    the recorded ref is the real committed base.
    """

    try:
        index = load_index(episode_root / RUN_DIR_NAME / SELECTION_PLAN_DIR_NAME)
    except SelectionPlanStoreError as error:
        raise PolicyDerivationError(
            "selection-ref-unreadable", f"committed selection store unreadable: {error}"
        ) from error
    if not index.versions:
        raise PolicyDerivationError(
            "selection-ref-empty", "the committed selection store holds no version"
        )
    latest = max(int(label) for label in index.versions)
    entry = index.versions[str(latest)]
    return SelectionPlanRef(
        episode_id=episode_id,
        plan_version=f"v{latest}",
        plan_sha256=entry.plan_sha256,
        plan_artifact_id=entry.plan_artifact_id,
    )


def _kept_segments(rerun: SelectionRerun) -> frozenset[str]:
    keep_ids = {
        candidate.candidate_id
        for candidate in rerun.selection.candidates
        if candidate.intent == "keep"
    }
    return frozenset(
        link.segment_id
        for link in rerun.reconciled.segment_links
        if set(link.candidate_ids) & keep_ids
    )


def _policy_subtitle_ids(rerun: SelectionRerun, policy: AdoptedPolicyV1 | None) -> tuple[str, ...]:
    speech_ids = tuple(segment.segment_id for segment in rerun.speech)
    if policy is None:
        return speech_ids
    record = derive_policy_settings(policy)
    return select_policy_subtitles(
        record, speech_ids, _kept_segments(rerun)
    )


def derive_policy_plan(
    episode_root: Path, rerun: SelectionRerun, policy: AdoptedPolicyV1 | None = None
) -> EditPlan0C:
    """Derive the review-plane plan through the chain's own pure functions.

    Planner infeasibility, generation, compile, and projection failures are
    all existing validators — any of them fails the derivation with a typed
    error and nothing is committed.
    """

    facts = EditSourceFacts(
        source_id=rerun.pool.source_id,
        edit_source_sha=rerun.pool.edit_source_sha,
        total_frames=rerun.pool.total_frames,
    )
    try:
        solution = solve(planner_input(rerun.selection, rerun.pool, rerun.reconciled, facts))
    except ValueError as error:
        raise PolicyDerivationError("planner-input-invalid", str(error)) from error
    if not isinstance(solution, PlannerSolution):
        raise PolicyDerivationError(
            "planner-infeasible", f"{solution.kind}: {solution.explanation}"
        )
    try:
        ref = selection_base_ref(episode_root, rerun.selection.episode_id)
        subtitle_ids = set(_policy_subtitle_ids(rerun, policy))
        plan = generate(
            rerun.selection,
            solution,
            plan_ref=ref,
            plan_base_version=EDIT_BASE,
            producer=REAL_PRODUCER,
            fixture_only=False,
            audio_bindings=(),
        )
        # Return value discarded: the compile stage rebuilds the IR. The
        # call itself is the production-compile validator over the plan.
        compile_ir(
            plan,
            source_id=rerun.pool.source_id,
            total_frames=rerun.pool.total_frames,
            cue_source=cue_source_for(rerun.speech),
            policy=qc_policy(),
            episode_id=rerun.selection.episode_id,
        )
        plane = project_plan(
            plan,
            rerun.reconciled,
            episode_id=rerun.selection.episode_id,
            source_id=rerun.pool.source_id,
            total_frames=rerun.pool.total_frames,
            rate=RationalFrameRate(num=30, den=1),
            subtitles=tuple(
                ReviewSubtitle(
                    subtitle_id=f"st{position}",
                    segment_id=segment.segment_id,
                    text=segment.text,
                    start_frame=segment.start_frame,
                    end_frame=segment.end_frame,
                )
                for position, segment in enumerate(rerun.speech, start=1)
                if segment.segment_id in subtitle_ids
            ),
        )
    except PolicyDerivationError:
        raise
    except (EditPlanGenerationError, ProjectionError, CompileProductionError) as error:
        raise PolicyDerivationError(error.code, error.detail) from error
    except (ValueError, OSError, RuntimeError) as error:
        raise PolicyDerivationError("plan-derivation-failed", str(error)) from error
    return plane.plan


__all__ = [
    "PolicyDerivationError",
    "SelectionRerun",
    "SelectionRerunError",
    "derive_policy_plan",
    "rebuild_analysis",
    "rerun_director_with_policy",
    "selection_base_ref",
]
