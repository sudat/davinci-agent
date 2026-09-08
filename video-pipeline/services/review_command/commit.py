"""Spike-local versioned-file writer for Review Event commits.

Validates commands via the Todo-28 validator first, appends sealed events,
then writes immutable plan/IR version files plus the versions.json chain
index. No SQLite, Artifact Registry, leases, or Production approval ingress;
Phase 1 wraps the same pure reducer with Production commit authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from services.compile.phase0c import CompileError, apply_command
from services.foundation_io import atomic_write, canonical_model_bytes
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    POLICY_EVENT_KIND,
    RESTORED_EVENT_KIND,
    ReviewEvent0C,
    build_event,
)
from services.review_command.models import (
    ApproveEditorialPlanProposal0C,
    ApproveRemainingProposal0C,
    ReviewCommandProposal0C,
)
from services.review_command.reducer import reduce, to_review_command, version_ir
from services.review_command.store import (
    INDEX_NAME,
    HeadState,
    OperatorDecision0C,
    PlanVersionsIndex,
    ReviewCommitError,
    VersionEntry,
    append_events,
    load_events,
    load_head,
    load_index,
    load_version_plan,
    sha256_bytes,
)
from services.review_command.validate import ProposalValidationError, validate_proposal


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    version: int
    deferred: bool
    reason: str | None
    idempotent: bool
    event_id: str | None
    plan_path: Path | None
    ir_path: Path | None


def _anchor(events: tuple[ReviewEvent0C, ...]) -> tuple[int, str]:
    if not events:
        return 0, GENESIS_EVENT_HASH
    return events[-1].sequence, events[-1].event_id


def _recorded_event(
    proposal: ReviewCommandProposal0C, sequence: int, previous_hash: str, base: str
) -> ReviewEvent0C:
    return build_event(
        sequence=sequence,
        kind="proposal_recorded",
        proposal=proposal,
        base_plan_version=base,
        previous_event_hash=previous_hash,
        actor_intent=proposal.actor_intent,
    )


def _idempotent_outcome(
    head: HeadState, proposal_sha256: str, plan_dir: Path
) -> CommitOutcome | None:
    for event in head.events:
        if event.proposal_sha256 != proposal_sha256:
            continue
        if event.kind == "decision_applied":
            version = int(event.result_plan_version[1:]) if event.result_plan_version else 0
            return CommitOutcome(
                version=version, deferred=False, reason=None, idempotent=True,
                event_id=event.event_id, plan_path=plan_dir / f"plan-v{version}.json",
                ir_path=plan_dir / f"ir-v{version}.json",
            )
        if event.kind == "command_deferred":
            return CommitOutcome(
                version=head.version, deferred=True, reason=event.reason, idempotent=True,
                event_id=event.event_id, plan_path=None, ir_path=None,
            )
    return None


def _deferred_outcome(
    proposal: ReviewCommandProposal0C,
    decision: OperatorDecision0C,
    log_path: Path,
    head: HeadState,
    reason: str,
) -> CommitOutcome:
    last_sequence, last_hash = _anchor(head.events)
    base = f"v{head.version}"
    recorded = _recorded_event(proposal, last_sequence + 1, last_hash, base)
    deferred = build_event(
        sequence=last_sequence + 2,
        kind="command_deferred",
        proposal=proposal,
        base_plan_version=base,
        previous_event_hash=recorded.event_id,
        actor_intent=decision.actor_intent,
        decision_id=decision.decision_id,
        reason=reason,
    )
    append_events(log_path, (recorded, deferred))
    return CommitOutcome(
        version=head.version, deferred=True, reason=reason, idempotent=False,
        event_id=deferred.event_id, plan_path=None, ir_path=None,
    )


def commit_command(
    proposal: ReviewCommandProposal0C,
    decision: OperatorDecision0C,
    events_log_path: Path,
    plan_dir: Path,
) -> CommitOutcome:
    if decision.actor_intent != "operator":
        raise ReviewCommitError(
            "model_authored_decision",
            "models propose; only operator decisions may commit",
        )
    head = load_head(events_log_path, plan_dir)
    proposal_sha256 = sha256_bytes(canonical_model_bytes(proposal))
    replay = _idempotent_outcome(head, proposal_sha256, plan_dir)
    if replay is not None:
        return replay

    base = f"v{head.version}"
    last_sequence, last_hash = _anchor(head.events)
    try:
        outcome = validate_proposal(head.plan, proposal)
    except ProposalValidationError as error:
        outcome = None
        reason: str | None = error.code
    else:
        reason = None if outcome.required_action == "apply" else outcome.classification
    if reason is not None:
        return _deferred_outcome(proposal, decision, events_log_path, head, reason)
    if outcome is None:
        raise ReviewCommitError("apply_failed", "validation produced neither outcome nor reason")

    if isinstance(proposal, ApproveRemainingProposal0C | ApproveEditorialPlanProposal0C):
        new_plan = head.plan
    else:
        try:
            new_plan = apply_command(
                head.plan, to_review_command(head.plan, proposal, outcome.candidate_item_ids)
            )
        except (CompileError, ValueError) as error:
            raise ReviewCommitError("apply_failed", str(error)) from error
    new_version = head.version + 1
    plan_path = plan_dir / f"plan-v{new_version}.json"
    ir_path = plan_dir / f"ir-v{new_version}.json"
    if plan_path.exists() or ir_path.exists():
        raise ReviewCommitError(
            "version_exists",
            f"version v{new_version} already exists; version files are immutable",
        )
    recorded = _recorded_event(proposal, last_sequence + 1, last_hash, base)
    applied_event = build_event(
        sequence=last_sequence + 2,
        kind="decision_applied",
        proposal=proposal,
        base_plan_version=base,
        result_plan_version=f"v{new_version}",
        applied=True,
        actor_intent=decision.actor_intent,
        decision_id=decision.decision_id,
        previous_event_hash=recorded.event_id,
    )
    append_events(events_log_path, (recorded, applied_event))
    plan_bytes = canonical_model_bytes(new_plan)
    ir_bytes = canonical_model_bytes(version_ir(head.base_plan, new_plan, new_version))
    atomic_write(plan_path, plan_bytes)
    atomic_write(ir_path, ir_bytes)
    entries = dict(head.index.versions)
    entries[str(new_version)] = VersionEntry(
        plan_sha256=sha256_bytes(plan_bytes),
        ir_sha256=sha256_bytes(ir_bytes),
        event_id=applied_event.event_id,
        parent_version=head.version,
    )
    index = PlanVersionsIndex(
        schema_version="plan-versions-v1",
        base_artifact_id=head.index.base_artifact_id,
        versions=entries,
    )
    atomic_write(plan_dir / INDEX_NAME, canonical_model_bytes(index))
    return CommitOutcome(
        version=new_version, deferred=False, reason=None, idempotent=False,
        event_id=applied_event.event_id, plan_path=plan_path, ir_path=ir_path,
    )


def recover_orphan(log_path: Path, plan_dir: Path) -> None:
    """Complete version files for applied events left orphaned by a crash."""

    index = load_index(plan_dir)
    base_plan = load_version_plan(plan_dir, index, 1)
    events = load_events(log_path)
    entries = dict(index.versions)
    for position, event in enumerate(events, start=1):
        if event.kind not in ("decision_applied", RESTORED_EVENT_KIND, POLICY_EVENT_KIND):
            continue
        label = event.result_plan_version
        if label is None:
            raise ReviewCommitError("broken_stream", "applied event lacks a result version")
        version = int(label[1:])
        if str(version) in entries:
            continue
        folded = reduce(events[:position], base_plan).plan
        plan_bytes = canonical_model_bytes(folded)
        ir_bytes = canonical_model_bytes(version_ir(base_plan, folded, version))
        for path, payload in (
            (plan_dir / f"plan-v{version}.json", plan_bytes),
            (plan_dir / f"ir-v{version}.json", ir_bytes),
        ):
            if path.exists():
                if path.read_bytes() != payload:
                    raise ReviewCommitError(
                        "foreign_plan",
                        f"{path.name} does not match the replayed version bytes",
                    )
            else:
                atomic_write(path, payload)
        entries[str(version)] = VersionEntry(
            plan_sha256=sha256_bytes(plan_bytes),
            ir_sha256=sha256_bytes(ir_bytes),
            event_id=event.event_id,
            parent_version=version - 1,
        )
    if len(entries) != len(index.versions):
        recovered = PlanVersionsIndex(
            schema_version="plan-versions-v1",
            base_artifact_id=index.base_artifact_id,
            versions=entries,
        )
        atomic_write(plan_dir / INDEX_NAME, canonical_model_bytes(recovered))
