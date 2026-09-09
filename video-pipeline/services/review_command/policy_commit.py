"""Operator policy commit (consultation slice 2): wholesale plan versions.

A director re-run under an adopted consultation policy yields a fully
re-derived plan that cannot be expressed as remove/adjust deltas, so it
commits as ONE self-contained ``policy_applied`` event carrying the
judgment linkage plus the full derived plan (the ``plan_restored`` shape).
Write order matches ``commit_command`` (events → files → index); history
is never rewritten and no AppliedCommand is written (a policy commit is
not a review command). A failed derivation commits nothing at all.

Slice2 P1: the commit runs orphan recovery first, enforces the reserved
base version + plan hash (CAS), returns the sealed event idempotently for
an identical re-commit, refuses a same-judgment different-plan re-commit
before writing, and reports a mid-commit file failure honestly: a
recovered version returns idempotently, a sealed-but-unrecovered event
names its recorded version (never a blanket "not reflected"), and only a
seal-absent failure reports no version.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import atomic_write, canonical_model_bytes
from services.review_command.commit import CommitOutcome, recover_orphan
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    POLICY_EVENT_KIND,
    EventActor0C,
    EventStreamError,
    ReviewEvent0C,
    compute_event_id,
    parse_policy_payload,
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


class PolicyRecoveryError(ReviewCommitError):
    """A mid-commit file failure with honest version reporting.

    ``recorded_version`` is the sealed event's result version when one
    exists (recovery unconfirmed — never "not reflected"); None when no
    policy event was sealed (no version exists).
    """

    def __init__(
        self, code: str, detail: str, *, recorded_version: int | None
    ) -> None:
        super().__init__(code, detail)
        self.recorded_version = recorded_version


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


def find_policy_event(
    events: tuple[ReviewEvent0C, ...], judgment_id: str
) -> ReviewEvent0C | None:
    """The latest sealed ``policy_applied`` event for one judgment, if any."""

    for event in reversed(events):
        if event.kind != POLICY_EVENT_KIND:
            continue
        try:
            payload = parse_policy_payload(event)
        except EventStreamError:
            continue
        if payload.judgment_id == judgment_id:
            return event
    return None


def _plan_sha256(plan: EditPlan0C) -> str:
    return sha256_bytes(canonical_model_bytes(plan))


def reuse_committed_policy(
    events_log_path: Path, plan_dir: Path, judgment_id: str
) -> CommitOutcome | None:
    """Return the already-sealed commit for a judgment without re-running.

    Crash-resume path: recover orphans first, then verify the sealed
    event's version is indexed with its files present. None means no
    exact commit exists — the caller proceeds to derive and commit.
    """

    try:
        recover_orphan(events_log_path, plan_dir)
        head = load_head(events_log_path, plan_dir)
    except (OSError, ReviewCommitError):
        return None
    event = find_policy_event(head.events, judgment_id)
    if event is None or event.result_plan_version is None:
        return None
    version = int(event.result_plan_version[1:])
    entry = head.index.versions.get(str(version))
    if entry is None or entry.event_id != event.event_id:
        return None
    plan_path = plan_dir / f"plan-v{version}.json"
    ir_path = plan_dir / f"ir-v{version}.json"
    if not plan_path.is_file() or not ir_path.is_file():
        return None
    return CommitOutcome(
        version=version,
        deferred=False,
        reason=None,
        idempotent=True,
        event_id=event.event_id,
        plan_path=plan_path,
        ir_path=ir_path,
    )


def commit_policy(  # noqa: PLR0913 (explicit append-only commit fields; kwargs are the contract)
    new_plan: EditPlan0C,
    decision: OperatorDecision0C,
    events_log_path: Path,
    plan_dir: Path,
    *,
    judgment_id: str,
    proposal_id: str | None,
    policy_decision: str,
    expected_base_version: str | None = None,
    expected_base_plan_sha256: str | None = None,
) -> CommitOutcome:
    """Commit a policy-derived plan as a new version (one apply-step)."""

    if decision.actor_intent != "operator":
        raise ReviewCommitError(
            "model_authored_decision",
            "models propose; only operator decisions may commit a policy plan",
        )
    recover_orphan(events_log_path, plan_dir)
    head = load_head(events_log_path, plan_dir)
    existing = find_policy_event(head.events, judgment_id)
    if existing is not None:
        payload = parse_policy_payload(existing)
        if (
            payload.proposal_id == proposal_id
            and payload.decision == policy_decision
            and _plan_sha256(payload.plan) == _plan_sha256(new_plan)
            and existing.result_plan_version is not None
        ):
            version = int(existing.result_plan_version[1:])
            return CommitOutcome(
                version=version,
                deferred=False,
                reason=None,
                idempotent=True,
                event_id=existing.event_id,
                plan_path=plan_dir / f"plan-v{version}.json",
                ir_path=plan_dir / f"ir-v{version}.json",
            )
        raise ReviewCommitError(
            "policy-idempotency-conflict",
            f"judgment {judgment_id} already committed a different "
            "proposal/decision/plan; refusing to overwrite",
        )
    if expected_base_version is not None:
        entry = head.index.versions.get(str(head.version))
        if (
            f"v{head.version}" != expected_base_version
            or entry is None
            or entry.plan_sha256 != expected_base_plan_sha256
        ):
            raise ReviewCommitError(
                "policy-base-version-changed",
                "予約後に編集の版が変わりました。古い版を上書きしていません。",
            )
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
    try:
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
    except (OSError, ReviewCommitError) as error:
        recovered = _recovered_outcome(
            events_log_path, plan_dir, judgment_id, new_version, event
        )
        if recovered is not None:
            return recovered
        raise _honest_recovery_error(
            events_log_path, plan_dir, judgment_id, event
        ) from error
    return CommitOutcome(
        version=new_version,
        deferred=False,
        reason=None,
        idempotent=False,
        event_id=event.event_id,
        plan_path=plan_path,
        ir_path=ir_path,
    )


def _recovered_outcome(
    events_log_path: Path,
    plan_dir: Path,
    judgment_id: str,
    new_version: int,
    event: ReviewEvent0C,
) -> CommitOutcome | None:
    """The sealed event's expected version, rebuilt by orphan recovery."""

    try:
        recover_orphan(events_log_path, plan_dir)
        head = load_head(events_log_path, plan_dir)
    except (OSError, ReviewCommitError):
        return None
    sealed = find_policy_event(head.events, judgment_id)
    if sealed is None or sealed.event_id != event.event_id:
        return None
    if head.version != new_version or str(new_version) not in head.index.versions:
        return None
    return CommitOutcome(
        version=new_version,
        deferred=False,
        reason=None,
        idempotent=True,
        event_id=sealed.event_id,
        plan_path=plan_dir / f"plan-v{new_version}.json",
        ir_path=plan_dir / f"ir-v{new_version}.json",
    )


def _honest_recovery_error(
    events_log_path: Path,
    plan_dir: Path,
    judgment_id: str,
    event: ReviewEvent0C,
) -> PolicyRecoveryError:
    try:
        head = load_head(events_log_path, plan_dir)
    except (OSError, ReviewCommitError):
        head = None
    sealed = find_policy_event(head.events, judgment_id) if head is not None else None
    if sealed is None:
        sealed = event if _event_sealed(events_log_path, event) else None
    if sealed is not None and sealed.result_plan_version is not None:
        recorded = int(sealed.result_plan_version[1:])
        return PolicyRecoveryError(
            "policy-commit-recovery-failed",
            f"編集の記録は v{recorded} まで残っていますが、"
            "版ファイルの回復を確認できません。自動で「反映されていない」とは判定しません。",
            recorded_version=recorded,
        )
    return PolicyRecoveryError(
        "policy-commit-failed",
        "版の確定前に失敗しました。方針を反映した版はありません。",
        recorded_version=None,
    )


def _event_sealed(events_log_path: Path, event: ReviewEvent0C) -> bool:
    try:
        raw = events_log_path.read_bytes()
    except OSError:
        return False
    return canonical_model_bytes(event) in raw


__all__ = [
    "PolicyRecoveryError",
    "build_policy_event",
    "commit_policy",
    "find_policy_event",
    "reuse_committed_policy",
]
