"""Multi-command apply: saved drafts → applied commands → ONE union rebuild.

V44-1 operator fix: one message may yield SEVERAL drafts, and the apply
route applies them ALL in order. Adoption authority is the SERVER-SAVED
proposal set (``review_proposals.resolve_authoritative_drafts``, brief
§5.3): only drafts that field-for-field equal a saved, unconsumed set
reach this module, so a client can mint no command the pipeline never
previewed. Every draft then goes through the SAME ``apply_command``
(sealed 0C event + immutable plan version per command), and the
response's rebuild plan carries the UNION of the per-command lineage
stage sets, ordered by ``PIPELINE_STAGES``, so the rebuild runner is
spawned ONCE for the whole batch.

工程2 rework round 2 (P1-1): validation is a SEQUENTIAL SIMULATION.
Draft i+1 is validated against the provisional plan AFTER drafts 1..i
applied (each commit moves the plan a later command names), so a bundle
is fully verified before the first write. Any validation failure applies
nothing at all; a commit that still fails (IO-level) restores the
pre-bundle version as a new honest version and reports exactly what
happened.

工程2 rework round 3: (P1-1) a BUNDLE treats any PREDICTED defer — a
validation error or a non-apply classification such as a lock conflict —
as a whole-bundle validation failure (typed 422 naming the command,
position, and why; ZERO commits; the saved set stays unconsumed). A
single-command apply keeps the honest defer (recorded intent, deferred
flag). (P1-2) the failure boundary now includes the applied-records
journal write: a journal OSError is a mid-commit failure — restore +
typed 4xx — and a failing RESTORE reports both failures plus the
loadable head version. Never a bare 500.
"""

# allow: SIZE_OK — the atomicity contract is ONE concept: sequential
# simulation, the single failure boundary, and the restore path must read
# together; splitting the boundary from the loop it guards scatters the
# all-or-nothing invariant this module exists to enforce.

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from services.compile.phase0c import CompileError
from services.compile.phase0c import apply_command as compile_apply_command
from services.episode_cockpit.review_chat import (
    DEFAULT_LINEAGE,
    PIPELINE_STAGES,
    AppliedCommand,
    RebuildPlan,
    ReviewChatError,
    ReviewCommandDraft,
    ReviewStoreLocation,
    _prepare_commit,
    apply_command,
    plan_rebuild,
    record_applied_command,
)
from services.review_command.commit import recover_orphan
from services.review_command.reducer import to_review_command
from services.review_command.restore import commit_restore
from services.review_command.store import (
    OperatorDecision0C,
    ReviewCommitError,
    load_head,
)
from services.review_command.validate import ProposalValidationError, validate_proposal

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.review_command.models import EditCommandProposal0C


def ordered_stage_union(plans: list[RebuildPlan]) -> tuple[str, ...]:
    """Union of stage sets in ``PIPELINE_STAGES`` order (deterministic)."""

    union = set[str]().union(*(set(plan.stages) for plan in plans))
    return tuple(stage for stage in PIPELINE_STAGES if stage in union)


def union_rebuild_plan(plans: list[RebuildPlan]) -> RebuildPlan:
    """One plan covering every applied command; the first command is the
    named primary (the per-command plans ride in the applied log)."""

    primary = plans[0]
    stages = ordered_stage_union(plans)
    return primary.model_copy(
        update={
            "stages": stages,
            "excluded_stages": tuple(
                stage for stage in PIPELINE_STAGES if stage not in stages
            ),
        }
    )


def _provisional_effect(
    plan: EditPlan0C, proposal: EditCommandProposal0C
) -> tuple[EditPlan0C, bool, str | None]:
    """The pure half of ``commit_command`` (validate → apply, NO writes).

    A deferred classification commits no version (plan unchanged), exactly
    like commit_command; the returned reason states what commit_command
    WOULD defer on (rework round 3 P1-1: a BUNDLE treats any predicted
    defer as whole-bundle validation failure, while a single command keeps
    the honest deferred commit). A CompileError is the same apply_failed
    failure commit_command would raise — deterministic, so it fails here,
    before any write, at the failing command's position."""

    try:
        outcome = validate_proposal(plan, proposal)
    except ProposalValidationError as error:
        return plan, False, f"{error.code}: {error.detail}"
    if outcome.required_action != "apply":
        why = outcome.classification
        if outcome.conflict is not None:
            why = (
                f"{outcome.classification} (target {outcome.conflict.target_item_id}"
                f" locks field {outcome.conflict.locked_field})"
            )
        elif outcome.ambiguity_reasons:
            why = f"{outcome.classification} ({'; '.join(outcome.ambiguity_reasons)})"
        return plan, False, why
    try:
        new_plan = compile_apply_command(
            plan, to_review_command(plan, proposal, outcome.candidate_item_ids)
        )
    except (CompileError, ValueError) as error:
        raise ReviewChatError("apply_failed", str(error)) from error
    return new_plan, True, None


def _bundle_restore(store: ReviewStoreLocation, bundle_base_version: int) -> int:
    """Commit the pre-bundle plan content back as a NEW version (honest
    trail: one ``plan_restored`` event, like the operator revert)."""

    decision = OperatorDecision0C(
        decision_id=f"dec-bundle-restore-v{bundle_base_version}",
        actor_intent="operator",
        note=f"restore plan v{bundle_base_version} (failed bundle rollback)",
    )
    outcome = commit_restore(bundle_base_version, decision, store.log_path, store.plan_dir)
    return outcome.version


def _error_label(error: BaseException) -> str:
    """``code: detail`` for typed errors, ``TypeName: message`` otherwise."""

    code = getattr(error, "code", None) or type(error).__name__
    detail = getattr(error, "detail", None) or str(error)
    return f"{code}: {detail}"


def _raise_bundle_commit_failed(  # noqa: PLR0913, PLR0917 (the failure context IS the contract: error, drafts, position, applied count, store, base version, stage)
    error: ReviewChatError | ReviewCommitError | OSError,
    drafts: list[ReviewCommandDraft],
    position: int,
    applied_count: int,
    store: ReviewStoreLocation,
    bundle_base_version: int,
    *,
    journal_failed: bool,
) -> None:
    """Honest mid-commit failure: recover any orphaned write, restore the
    pre-bundle version when the head moved, and surface applied N / failed
    at N+1 / restored-to vN in the typed error detail. The boundary covers
    the applied-records JOURNAL write too (rework round 3 P1-2): a journal
    OSError means the commit landed but the audit entry did not — the same
    restore path runs. A failing RESTORE is caught and reported together
    with the original failure, plus the loadable head version read
    afterwards; this helper NEVER lets a bare OSError reach HTTP as 500."""

    restored: int | None = None
    rollback: str | None = None
    try:
        recover_orphan(store.log_path, store.plan_dir)
        head_now = load_head(store.log_path, store.plan_dir).version
        if head_now > bundle_base_version:
            restored = _bundle_restore(store, bundle_base_version)
    except (ReviewCommitError, OSError) as rollback_error:
        rollback = _error_label(rollback_error)
    if journal_failed:
        what = (
            f"command {position + 1} of {len(drafts)} ({drafts[position].command_id})"
            " committed but writing its applied-command journal entry failed after"
            f" {applied_count} of {len(drafts)} commands were journaled"
        )
    else:
        what = (
            f"command {position + 1} of {len(drafts)} ({drafts[position].command_id})"
            f" failed at commit after {applied_count} of {len(drafts)} commands were"
            " applied"
        )
    parts = [what, f"({_error_label(error)})"]
    if restored is not None:
        parts.append(f"plan restored to v{restored}")
    elif rollback is None:
        parts.append(f"the plan head never moved past v{bundle_base_version}")
    else:
        parts.append(f"rollback failed ({rollback})")
        try:
            head = load_head(store.log_path, store.plan_dir)
        except (ReviewCommitError, OSError) as read_error:
            parts.append(f"plan head unreadable ({_error_label(read_error)})")
        else:
            parts.append(f"current plan head is v{head.version}")
    raise ReviewChatError("bundle-commit-failed", "; ".join(parts)) from error


def apply_drafts(
    drafts: list[ReviewCommandDraft], *, episode_dir: Path, store: ReviewStoreLocation
) -> tuple[list[AppliedCommand], RebuildPlan]:
    """Apply each confirmed draft in order; ONE union rebuild plan back.

    工程2 atomicity (brief §5.3: 複数修正の採用前に全体を検証し、失敗時に
    半分だけ採用済みにしない): the bundle is SIMULATED sequentially before
    any commit — draft i+1 against the provisional plan drafts 1..i
    produced — so validation failure applies nothing, records nothing,
    and leaves the saved set unconsumed (the caller never reaches
    consumption). The commit phase re-runs the SAME machinery against the
    store, where the head now equals the simulated state at each step;
    a failure there (IO-level only) restores the pre-bundle version and
    reports the exact state honestly."""

    if not drafts:
        raise ReviewChatError("drafts-empty", "at least one draft is required")
    head = load_head(store.log_path, store.plan_dir)
    bundle_base_version = head.version
    simulated = head
    bundle = len(drafts) > 1
    for index, draft in enumerate(drafts):
        try:
            prepared = _prepare_commit(draft, store=store, head=simulated)
            if prepared is None:
                continue
            new_plan, changed, defer_reason = _provisional_effect(simulated.plan, prepared[1])
        except ReviewChatError as error:
            raise ReviewChatError(
                error.code,
                f"command {index + 1} of {len(drafts)} ({draft.command_id}) "
                f"cannot be applied: {error.detail}; nothing was applied",
            ) from error
        if changed:
            simulated = replace(simulated, plan=new_plan, version=simulated.version + 1)
        elif bundle:
            # P1-1 (rework round 3): bundle = all-or-nothing; a single
            # command keeps commit_command's honest deferred intent instead.
            raise ReviewChatError(
                "bundle-command-deferred",
                f"command {index + 1} of {len(drafts)} ({draft.command_id}) would be"
                f" deferred ({defer_reason}); a command bundle applies all-or-nothing,"
                " so nothing was committed and nothing was applied",
            )
    applied: list[AppliedCommand] = []
    for position, draft in enumerate(drafts):
        command: AppliedCommand | None = None
        try:
            command = apply_command(draft, store=store)
            # P1-2 (rework round 3): journal write is INSIDE the boundary.
            record_applied_command(episode_dir, command)
        except (ReviewChatError, ReviewCommitError, OSError) as error:
            _raise_bundle_commit_failed(
                error,
                drafts,
                position,
                len(applied),
                store,
                bundle_base_version,
                journal_failed=command is not None,
            )
            raise  # unreachable: the helper always raises
        applied.append(command)
    plans = [plan_rebuild(command, DEFAULT_LINEAGE) for command in applied]
    return applied, union_rebuild_plan(plans)


__all__ = [
    "apply_drafts",
    "ordered_stage_union",
    "union_rebuild_plan",
]
