"""Operator restore (UX redesign 工程1): re-commit a previous plan version.

Going back is a NEW forward version whose content equals the parent's,
recorded by ONE self-contained ``plan_restored`` event — history is never
rewritten and no AppliedCommand is written (a restore is not a command).
The event payload carries the restored-from version plus the restored plan
(``events.restore_payload``), so the reducer folds restores without disk
reads and ``recover_orphan`` rebuilds an interrupted restore like any other
version. Write order matches ``commit_command`` (events → files → index).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from services.foundation_io import atomic_write, canonical_model_bytes
from services.review_command.commit import CommitOutcome
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    RESTORED_EVENT_KIND,
    EventActor0C,
    ReviewEvent0C,
    compute_event_id,
    restore_payload,
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
    load_version_plan,
    sha256_bytes,
)


def build_restore_event(  # noqa: PLR0913 (explicit append-only event fields; kwargs are the contract)
    *,
    sequence: int,
    base_plan_version: str,
    result_plan_version: str,
    previous_event_hash: str,
    actor_intent: EventActor0C,
    decision_id: str,
    restored_plan_json: str,
) -> ReviewEvent0C:
    draft = ReviewEvent0C(
        event_id=GENESIS_EVENT_HASH,
        sequence=sequence,
        kind=RESTORED_EVENT_KIND,
        proposal_json=restored_plan_json,
        proposal_sha256=hashlib.sha256(restored_plan_json.encode()).hexdigest(),
        base_plan_version=base_plan_version,
        result_plan_version=result_plan_version,
        applied=True,
        actor_intent=actor_intent,
        decision_id=decision_id,
        previous_event_hash=previous_event_hash,
    )
    return draft.model_copy(update={"event_id": compute_event_id(draft)})


def commit_restore(
    restored_from_version: int,
    decision: OperatorDecision0C,
    events_log_path: Path,
    plan_dir: Path,
) -> CommitOutcome:
    """Commit the parent plan's content as a new version (one apply-step)."""

    if decision.actor_intent != "operator":
        raise ReviewCommitError(
            "model_authored_decision",
            "models propose; only operator decisions may restore a plan version",
        )
    head = load_head(events_log_path, plan_dir)
    if not 1 <= restored_from_version < head.version:
        raise ReviewCommitError(
            "restore-version-invalid",
            f"cannot restore v{restored_from_version} from head v{head.version}",
        )
    source_plan = load_version_plan(plan_dir, head.index, restored_from_version)
    new_version = head.version + 1
    plan_path = plan_dir / f"plan-v{new_version}.json"
    ir_path = plan_dir / f"ir-v{new_version}.json"
    if plan_path.exists() or ir_path.exists():
        raise ReviewCommitError(
            "version_exists",
            f"version v{new_version} already exists; version files are immutable",
        )
    last = head.events[-1] if head.events else None
    event = build_restore_event(
        sequence=(last.sequence + 1) if last else 1,
        base_plan_version=f"v{head.version}",
        result_plan_version=f"v{new_version}",
        previous_event_hash=last.event_id if last else GENESIS_EVENT_HASH,
        actor_intent=decision.actor_intent,
        decision_id=decision.decision_id,
        restored_plan_json=restore_payload(restored_from_version, source_plan),
    )
    append_events(events_log_path, (event,))
    plan_bytes = canonical_model_bytes(source_plan)
    ir_bytes = canonical_model_bytes(version_ir(head.base_plan, source_plan, new_version))
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


__all__ = ["build_restore_event", "commit_restore"]
