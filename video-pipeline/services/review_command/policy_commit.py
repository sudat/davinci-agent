"""Operator policy commit (consultation slice 2): wholesale plan versions.

A director re-run under an adopted consultation policy yields a fully
re-derived plan that cannot be expressed as remove/adjust deltas, so it
commits as ONE self-contained ``policy_applied`` event carrying the
judgment linkage plus the full derived plan (the ``plan_restored`` shape).
Write order matches ``commit_command`` (events → files → index); history
is never rewritten and no AppliedCommand is written (a policy commit is
not a review command). A failed derivation commits nothing at all.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import atomic_write, canonical_model_bytes
from services.review_command.commit import CommitOutcome
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    POLICY_EVENT_KIND,
    EventActor0C,
    ReviewEvent0C,
    compute_event_id,
    policy_payload,
)
from services.review_command.reducer import version_ir
from services.review_command.store import (
    INDEX_NAME,
    OperatorDecision0C,
    PlanVersionsIndex,
    ReviewCommitError,
    VersionEntry,
    append_events,
    load_head,
    sha256_bytes,
)

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C

def build_policy_event(  # noqa: PLR0913 (explicit append-only event fields; kwargs are the contract)
    *,
    sequence: int,
    base_plan_version: str,
    result_plan_version: str,
    previous_event_hash: str,
    actor_intent: EventActor0C,
    decision_id: str,
    policy_json: str,
) -> ReviewEvent0C:
    draft = ReviewEvent0C(
        event_id=GENESIS_EVENT_HASH,
        sequence=sequence,
        kind=POLICY_EVENT_KIND,
        proposal_json=policy_json,
        proposal_sha256=hashlib.sha256(policy_json.encode()).hexdigest(),
        base_plan_version=base_plan_version,
        result_plan_version=result_plan_version,
        applied=True,
        actor_intent=actor_intent,
        decision_id=decision_id,
        previous_event_hash=previous_event_hash,
    )
    return draft.model_copy(update={"event_id": compute_event_id(draft)})


def commit_policy(  # noqa: PLR0913 (explicit append-only commit fields; kwargs are the contract)
    new_plan: EditPlan0C,
    decision: OperatorDecision0C,
    events_log_path: Path,
    plan_dir: Path,
    *,
    judgment_id: str,
    proposal_id: str | None,
    policy_decision: str,
) -> CommitOutcome:
    """Commit a policy-derived plan as a new version (one apply-step)."""

    if decision.actor_intent != "operator":
        raise ReviewCommitError(
            "model_authored_decision",
            "models propose; only operator decisions may commit a policy plan",
        )
    head = load_head(events_log_path, plan_dir)
    new_version = head.version + 1
    plan_path = plan_dir / f"plan-v{new_version}.json"
    ir_path = plan_dir / f"ir-v{new_version}.json"
    if plan_path.exists() or ir_path.exists():
        raise ReviewCommitError(
            "version_exists",
            f"version v{new_version} already exists; version files are immutable",
        )
    last = head.events[-1] if head.events else None
    event = build_policy_event(
        sequence=(last.sequence + 1) if last else 1,
        base_plan_version=f"v{head.version}",
        result_plan_version=f"v{new_version}",
        previous_event_hash=last.event_id if last else GENESIS_EVENT_HASH,
        actor_intent=decision.actor_intent,
        decision_id=decision.decision_id,
        policy_json=policy_payload(judgment_id, proposal_id, policy_decision, new_plan),
    )
    append_events(events_log_path, (event,))
    plan_bytes = canonical_model_bytes(new_plan)
    ir_bytes = canonical_model_bytes(version_ir(head.base_plan, new_plan, new_version))
    atomic_write(plan_path, plan_bytes)
    atomic_write(ir_path, ir_bytes)
    entries = dict(head.index.versions)
    entries[str(new_version)] = VersionEntry(
        plan_sha256=sha256_bytes(plan_bytes),
        ir_sha256=sha256_bytes(ir_bytes),
        event_id=event.event_id,
        parent_version=head.version,
    )
    index = PlanVersionsIndex(
        schema_version="plan-versions-v1",
        base_artifact_id=head.index.base_artifact_id,
        versions=entries,
    )
    atomic_write(plan_dir / INDEX_NAME, canonical_model_bytes(index))
    return CommitOutcome(
        version=new_version,
        deferred=False,
        reason=None,
        idempotent=False,
        event_id=event.event_id,
        plan_path=plan_path,
        ir_path=ir_path,
    )


__all__ = ["build_policy_event", "commit_policy"]
