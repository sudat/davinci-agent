"""Commit seams: refusal recording, replay detection, publish+adopt (Todo 41).

Shared by the commit authority: the sealed-event refusal record
(idempotent per digest and code), the replay scan, and the
publish/replay outcome paths — never regressing state.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import canonical_model_bytes
from services.review_command.events import GENESIS_EVENT_HASH
from services.validate.selection_events import SelectionCommitEvent, build_selection_event
from services.validate.selection_models import (
    CommittedSelectionPlan,
    SelectionCommitOutcome,
    SelectionCommitRefusal,
    ValidationRefusal,
)
from services.validate.selection_plan_store import (
    SelectionPlanVersionEntry,
    SelectionPlanVersionsIndex,
    append_events,
    latest_version,
    write_entry,
)

if TYPE_CHECKING:
    from services.editorial.candidate_models import SelectionPlanProposal
    from services.job_runner.cas import CasSnapshot
    from services.validate.selection_publisher import SelectionPublisher

class SelectionCommitError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def raw_digest(document: object) -> str:
    """Best-effort digest for documents that never parsed."""
    try:
        payload = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        payload = repr(document).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def anchor(events: tuple[SelectionCommitEvent, ...]) -> tuple[int, str]:
    if not events:
        return 0, GENESIS_EVENT_HASH
    return events[-1].sequence, events[-1].event_id


def event_version(event: SelectionCommitEvent) -> int:
    result = event.result_version
    if result is None:
        raise SelectionCommitError("broken_stream", "plan_committed event lacks a result version")
    return int(result[1:])


def replay_event(
    events: tuple[SelectionCommitEvent, ...], digest: str
) -> SelectionCommitEvent | None:
    for event in reversed(events):
        if event.kind == "plan_committed" and event.proposal_digest == digest:
            return event
    return None


def record_refusal(  # noqa: PLR0913, PLR0917 (sealed-log seam: refusal record inputs)
    plan_dir: Path,
    episode_id: str,
    events: tuple[SelectionCommitEvent, ...],
    digest: str,
    base_version: str | None,
    refusal: ValidationRefusal,
) -> SelectionCommitRefusal:
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
    append_events(plan_dir, (event,))
    return _refusal(event.event_id, refusal)


def _refusal(event_id: str, refusal: ValidationRefusal) -> SelectionCommitRefusal:
    return SelectionCommitRefusal(
        validator=refusal.validator,
        code=refusal.code,
        detail=refusal.detail,
        deferred=refusal.deferred,
        event_id=event_id,
    )


def plan_for_version(proposal: SelectionPlanProposal, index: SelectionPlanVersionsIndex,
                     version: int) -> CommittedSelectionPlan:
    base = index.base_version if version == 1 else f"v{version - 1}"
    return CommittedSelectionPlan(
        plan_version=f"v{version}",
        episode_id=index.episode_id,
        base_version=base,
        proposal=proposal,
    )


def publish_version(  # noqa: PLR0913 (commit seam: validators passed, publish and adopt)
    publisher: SelectionPublisher,
    plan_dir: Path,
    proposal: SelectionPlanProposal,
    *,
    index: SelectionPlanVersionsIndex,
    version: int,
    event: SelectionCommitEvent | None,
    events: tuple[SelectionCommitEvent, ...],
    digest: str,
    base_label: str,
    snapshot: CasSnapshot,
    now: int,
    ttl_seconds: int,
) -> SelectionCommitOutcome:
    plan = plan_for_version(proposal, index, version)
    plan_bytes = canonical_model_bytes(plan)
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
        append_events(plan_dir, (event,))
    plan_artifact_id = f"selection-plan.{index.episode_id}.v{version}"
    publisher.publish_proposal(proposal)
    publisher.publish_plan(plan_artifact_id, plan_sha, proposal, plan_bytes)
    write_entry(
        plan_dir,
        index,
        version,
        SelectionPlanVersionEntry(
            plan_sha256=plan_sha,
            plan_artifact_id=plan_artifact_id,
            proposal_sha256=digest,
            parent_version=None if version == 1 else version - 1,
            event_id=event.event_id,
        ),
    )
    adopted, changed = publisher.adopt(snapshot, plan_sha, now=now, ttl_seconds=ttl_seconds)
    return SelectionCommitOutcome(
        version=version,
        plan_sha256=plan_sha,
        plan_artifact_id=plan_artifact_id,
        proposal_artifact_id=proposal.proposal_id,
        event_id=event.event_id,
        idempotent=False,
        state_changed=changed,
        adopted=adopted,
    )


def replay_adopt(  # noqa: PLR0913 (replay seam: authority state passthrough)
    publisher: SelectionPublisher,
    proposal: SelectionPlanProposal,
    event: SelectionCommitEvent,
    entry: SelectionPlanVersionEntry,
    index: SelectionPlanVersionsIndex,
    *,
    snapshot: CasSnapshot,
    now: int,
    ttl_seconds: int,
) -> SelectionCommitOutcome:
    version = event_version(event)
    plan = plan_for_version(proposal, index, version)
    plan_bytes = canonical_model_bytes(plan)
    plan_sha = hashlib.sha256(plan_bytes).hexdigest()
    if plan_sha != entry.plan_sha256:
        raise SelectionCommitError(
            "foreign_event",
            f"replay of event {event.event_id} rebuilt bytes that do not match the"
            f" committed version v{version}",
        )
    publisher.publish_proposal(proposal)
    publisher.publish_plan(entry.plan_artifact_id, entry.plan_sha256, proposal, plan_bytes)
    if version != latest_version(index):
        return SelectionCommitOutcome(
            version=version,
            plan_sha256=entry.plan_sha256,
            plan_artifact_id=entry.plan_artifact_id,
            proposal_artifact_id=proposal.proposal_id,
            event_id=event.event_id,
            idempotent=True,
            state_changed=False,
            adopted=snapshot,
        )
    adopted, _changed = publisher.adopt(
        snapshot, entry.plan_sha256, now=now, ttl_seconds=ttl_seconds
    )
    return SelectionCommitOutcome(
        version=version,
        plan_sha256=entry.plan_sha256,
        plan_artifact_id=entry.plan_artifact_id,
        proposal_artifact_id=proposal.proposal_id,
        event_id=event.event_id,
        idempotent=True,
        state_changed=adopted.updated_at_seq != snapshot.updated_at_seq,
        adopted=adopted,
    )


__all__ = [
    "SelectionCommitError",
    "anchor",
    "event_version",
    "plan_for_version",
    "publish_version",
    "raw_digest",
    "record_refusal",
    "replay_adopt",
    "replay_event",
]
