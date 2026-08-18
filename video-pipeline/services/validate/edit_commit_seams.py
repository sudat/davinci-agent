"""Commit seams for Edit Plans: refusal recording, replay, publish+adopt (Todo 43).

The Todo-41 seam family adapted to the edit models (same sealed-event refusal
record idempotent per digest and code, the same replay scan, and the same
publish/replay outcome paths that never regress state) — reusing the generic
event/stream primitives from the selection machinery verbatim.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import canonical_model_bytes
from services.validate.edit_commit_models import (
    CommittedEditPlan,
    EditCommitOutcome,
    EditCommitRefusal,
    EditValidationRefusal,
)
from services.validate.edit_commit_store import (
    EditPlanVersionEntry,
    append_events,
    latest_edit_version,
    write_edit_entry,
)
from services.validate.selection_events import build_selection_event
from services.validate.selection_seams import (
    anchor,
    event_version,
    raw_digest,
    replay_event,
)

if TYPE_CHECKING:
    from services.job_runner.cas import CasSnapshot
    from services.plan.edit_plan_models import EditPlan
    from services.validate.edit_commit_publisher import EditPublisher
    from services.validate.edit_commit_store import EditPlanVersionsIndex
    from services.validate.selection_events import SelectionCommitEvent


class EditCommitError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def record_edit_refusal(  # noqa: PLR0913, PLR0917 (sealed-log seam: refusal inputs)
    edit_plan_dir: Path,
    episode_id: str,
    events: tuple[SelectionCommitEvent, ...],
    digest: str,
    base_version: str | None,
    refusal: EditValidationRefusal,
) -> EditCommitRefusal:
    """Record the refusal once per (digest, code); replays reuse the event id."""

    validator = refusal.validator if refusal.validator is not None else "commit"
    for event in reversed(events):
        if (
            event.kind == "commit_refused"
            and event.proposal_digest == digest
            and event.refusal_code == refusal.code
        ):
            return _refusal(event.event_id, refusal)
    sequence, previous = anchor(events)
    event = build_selection_event(
        sequence=sequence + 1,
        kind="commit_refused",
        episode_id=episode_id,
        proposal_digest=digest,
        proposal_base_version=base_version,
        previous_event_hash=previous,
        refusal_validator=validator,
        refusal_code=refusal.code,
    )
    append_events(edit_plan_dir, (event,))
    return _refusal(event.event_id, refusal)


def _refusal(event_id: str, refusal: EditValidationRefusal) -> EditCommitRefusal:
    return EditCommitRefusal(
        validator=refusal.validator,
        code=refusal.code,
        detail=refusal.detail,
        deferred=refusal.deferred,
        event_id=event_id,
    )


def committed_for_version(
    plan: EditPlan, index: EditPlanVersionsIndex, version: int
) -> CommittedEditPlan:
    base = index.base_version if version == 1 else f"v{version - 1}"
    return CommittedEditPlan(
        plan_version=f"v{version}",
        episode_id=index.episode_id,
        base_version=base,
        proposal=plan,
    )


def publish_edit_version(  # noqa: PLR0913 (commit seam: validators passed, publish and adopt)
    publisher: EditPublisher,
    edit_plan_dir: Path,
    plan: EditPlan,
    *,
    index: EditPlanVersionsIndex,
    version: int,
    event: SelectionCommitEvent | None,
    events: tuple[SelectionCommitEvent, ...],
    digest: str,
    base_label: str,
    snapshot: CasSnapshot,
    now: int,
    ttl_seconds: int,
) -> EditCommitOutcome:
    committed = committed_for_version(plan, index, version)
    plan_bytes = canonical_model_bytes(committed)
    plan_sha = hashlib.sha256(plan_bytes).hexdigest()
    if event is None:
        sequence, previous = anchor(events)
        event = build_selection_event(
            sequence=sequence + 1,
            kind="plan_committed",
            episode_id=index.episode_id,
            proposal_digest=digest,
            proposal_base_version=base_label,
            previous_event_hash=previous,
            result_version=f"v{version}",
        )
        append_events(edit_plan_dir, (event,))
    plan_artifact_id = f"edit-plan.{index.episode_id}.v{version}"
    publisher.publish_proposal(plan)
    publisher.publish_plan(plan_artifact_id, plan_sha, plan, plan_bytes)
    write_edit_entry(
        edit_plan_dir,
        index,
        version,
        EditPlanVersionEntry(
            plan_sha256=plan_sha,
            plan_artifact_id=plan_artifact_id,
            proposal_sha256=digest,
            parent_version=None if version == 1 else version - 1,
            event_id=event.event_id,
        ),
    )
    adopted, changed = publisher.adopt(snapshot, plan_sha, now=now, ttl_seconds=ttl_seconds)
    return EditCommitOutcome(
        version=version,
        plan_sha256=plan_sha,
        plan_artifact_id=plan_artifact_id,
        proposal_artifact_id=plan.proposal_id,
        event_id=event.event_id,
        idempotent=False,
        state_changed=changed,
        adopted=adopted,
    )


def replay_edit_adopt(  # noqa: PLR0913 (replay seam: authority state passthrough)
    publisher: EditPublisher,
    plan: EditPlan,
    event: SelectionCommitEvent,
    entry: EditPlanVersionEntry,
    index: EditPlanVersionsIndex,
    *,
    snapshot: CasSnapshot,
    now: int,
    ttl_seconds: int,
) -> EditCommitOutcome:
    version = event_version(event)
    committed = committed_for_version(plan, index, version)
    plan_bytes = canonical_model_bytes(committed)
    plan_sha = hashlib.sha256(plan_bytes).hexdigest()
    if plan_sha != entry.plan_sha256:
        raise EditCommitError(
            "foreign_event",
            f"replay of event {event.event_id} rebuilt bytes that do not match the"
            f" committed version v{version}",
        )
    publisher.publish_proposal(plan)
    publisher.publish_plan(entry.plan_artifact_id, entry.plan_sha256, plan, plan_bytes)
    if version != latest_edit_version(index):
        return EditCommitOutcome(
            version=version,
            plan_sha256=entry.plan_sha256,
            plan_artifact_id=entry.plan_artifact_id,
            proposal_artifact_id=plan.proposal_id,
            event_id=event.event_id,
            idempotent=True,
            state_changed=False,
            adopted=snapshot,
        )
    adopted, _changed = publisher.adopt(
        snapshot, entry.plan_sha256, now=now, ttl_seconds=ttl_seconds
    )
    return EditCommitOutcome(
        version=version,
        plan_sha256=entry.plan_sha256,
        plan_artifact_id=entry.plan_artifact_id,
        proposal_artifact_id=plan.proposal_id,
        event_id=event.event_id,
        idempotent=True,
        state_changed=adopted.updated_at_seq != snapshot.updated_at_seq,
        adopted=adopted,
    )


__all__ = [
    "EditCommitError",
    "committed_for_version",
    "publish_edit_version",
    "raw_digest",
    "record_edit_refusal",
    "replay_edit_adopt",
    "replay_event",
]
