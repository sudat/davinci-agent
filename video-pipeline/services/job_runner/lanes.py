"""Serialized mutation lanes over the single Job State authority (Todo 10).

Three explicit lanes — State, Plan/Event commit, Resolve Builder — each
acquire a NAMED exclusive lease from the Todo-9 SQL lease table before
mutating and release it after. Ordering contract: ``acquire lane lease →
lane mutation → state CAS → release state lease → release lane lease``;
a lane never holds more than its own lease plus the state lease. There
is NO distributed transaction: crash recovery is lease expiry (Todo-9
logical clock) plus CAS supersession — a late writer re-reads current
state inside the CAS transaction and is superseded without writing; an
orphan plan file left between the Todo-30 write and the state CAS is
detectable via the versions index and re-adopted by an idempotent re-run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.job_runner.cas import (
    CasSnapshot,
    TransitionPayload,
    apply_transition,
    current_job_state,
)
from services.job_runner.state_errors import StateStoreError
from services.review_command.commit import CommitOutcome, commit_command
from services.review_command.store import OperatorDecision0C, load_index

if TYPE_CHECKING:
    from pathlib import Path

    from services.job_runner.state_models import JobStatus
    from services.job_runner.state_store import StateStore
    from services.review_command.models import ReviewCommandProposal0C


def state_resource(job_id: str) -> Identifier:
    return f"state:{job_id}"


def plan_commit_resource(job_id: str) -> Identifier:
    return f"plan-commit:{job_id}"


def resolve_build_resource(job_id: str) -> Identifier:
    return f"resolve-build:{job_id}"


class _LaneLease:
    """One named exclusive lease; expiry is judged by the logical clock.

    Holders are invocation-unique tokens. A lane that re-enters
    ``acquire`` while it already holds a FRESH lease refreshes through
    ``renew_lease`` (the owner's only refresh path); acquisition itself
    never steals a fresh lease, not even from the same token.
    """

    def __init__(self, store: StateStore, holder: Identifier, resource: Identifier) -> None:
        self._store = store
        self._holder = holder
        self._resource = resource
        self._ttl = 0
        self._held = False

    def acquire(self, *, now: int, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        if self._held:
            self._store.renew_lease(
                resource=self._resource, holder=self._holder, now=now,
                ttl_seconds=ttl_seconds,
            )
            return
        self._store.acquire_lease(
            resource=self._resource, holder=self._holder, now=now, ttl_seconds=ttl_seconds
        )
        self._held = True

    def renew(self, *, now: int) -> None:
        self._store.renew_lease(
            resource=self._resource, holder=self._holder, now=now, ttl_seconds=self._ttl
        )

    def release(self, *, now: int) -> None:
        self._store.release_lease(resource=self._resource, holder=self._holder, now=now)
        self._held = False


class StateLane:
    """Lease-held wrapper around the CAS for job-state mutations."""

    def __init__(self, store: StateStore, job_id: Identifier, *, holder: Identifier) -> None:
        self._store = store
        self._job_id = job_id
        self._lease = _LaneLease(store, holder, state_resource(job_id))

    def acquire(self, *, now: int, ttl_seconds: int) -> None:
        self._lease.acquire(now=now, ttl_seconds=ttl_seconds)

    def apply(  # noqa: PLR0913 (CAS contract passthrough)
        self,
        *,
        expected_status: JobStatus,
        expected_parent_hash: Sha256 | None,
        new_status: JobStatus,
        new_artifact_hash: Sha256,
        payload: TransitionPayload | None = None,
        now: int,
    ) -> CasSnapshot:
        self._lease.renew(now=now)
        return apply_transition(
            self._store,
            self._job_id,
            expected_status=expected_status, expected_parent_hash=expected_parent_hash,
            new_status=new_status, new_artifact_hash=new_artifact_hash, payload=payload,
        )

    def release(self, *, now: int) -> None:
        self._lease.release(now=now)


@dataclass(frozen=True, slots=True)
class PlanCommitOutcome:
    commit: CommitOutcome
    state: CasSnapshot | None


class PlanCommitLane:
    """Serializes Todo-30 plan/event commits with one adoption CAS."""

    def __init__(self, store: StateStore, job_id: Identifier, *, holder: Identifier) -> None:
        self._store = store
        self._job_id = job_id
        self._holder = holder
        self._lease = _LaneLease(store, holder, plan_commit_resource(job_id))

    def acquire(self, *, now: int, ttl_seconds: int) -> None:
        self._lease.acquire(now=now, ttl_seconds=ttl_seconds)

    def release(self, *, now: int) -> None:
        self._lease.release(now=now)

    def commit(  # noqa: PLR0913 (lane contract: commit inputs + logical clock)
        self,
        *,
        proposal: ReviewCommandProposal0C,
        decision: OperatorDecision0C,
        events_log_path: Path,
        plan_dir: Path,
        now: int,
        ttl_seconds: int,
    ) -> PlanCommitOutcome:
        self.acquire(now=now, ttl_seconds=ttl_seconds)
        try:
            snapshot = current_job_state(self._store, self._job_id)
            if snapshot.status not in {"PLAN_PROPOSED", "PLAN_COMMITTED"}:
                raise StateStoreError(
                    "plan-commit-status",
                    f"job {self._job_id} is {snapshot.status}; plan commits run"
                    " only at PLAN_PROPOSED/PLAN_COMMITTED",
                )
            outcome = commit_command(proposal, decision, events_log_path, plan_dir)
            if outcome.deferred or outcome.version == 0:
                return PlanCommitOutcome(commit=outcome, state=None)
            plan_hash: Sha256 = load_index(plan_dir).versions[str(outcome.version)].plan_sha256
            if snapshot.status == "PLAN_COMMITTED" and plan_hash == snapshot.adopted_artifact_hash:
                return PlanCommitOutcome(commit=outcome, state=None)
            state_lane = StateLane(self._store, self._job_id, holder=self._holder)
            state_lane.acquire(now=now, ttl_seconds=ttl_seconds)
            try:
                adopted = state_lane.apply(
                    expected_status=snapshot.status,
                    expected_parent_hash=snapshot.adopted_artifact_hash,
                    new_status="PLAN_COMMITTED",
                    new_artifact_hash=plan_hash,
                    now=now,
                )
            finally:
                state_lane.release(now=now)
            return PlanCommitOutcome(commit=outcome, state=adopted)
        finally:
            self.release(now=now)


class BuildRequest(StrictModel):
    """Modeled build-request object; no live Resolve call exists here."""

    job_id: Identifier
    base_artifact_hash: Sha256
    requested_by: Identifier


class BuildAcceptance(StrictModel):
    job_id: Identifier
    base_artifact_hash: Sha256
    accepted_seq: int


class ResolveBuildLane:
    """Modeled Resolve Builder authority (no live Resolve calls here).

    Accepts build-request objects ONLY while holding the resolve-build
    lease AND the job re-reads as ``EDITORIAL_APPROVED`` with the
    request base equal to the currently adopted artifact.
    """

    def __init__(self, store: StateStore, job_id: Identifier, *, holder: Identifier) -> None:
        self._store = store
        self._job_id = job_id
        self._lease = _LaneLease(store, holder, resolve_build_resource(job_id))

    def open(self, *, now: int, ttl_seconds: int) -> ResolveBuildLane:
        self._lease.acquire(now=now, ttl_seconds=ttl_seconds)
        try:
            self._require_editorial_approved()
        except Exception:
            self.release(now=now)
            raise
        return self

    def accept(self, request: BuildRequest, *, now: int) -> BuildAcceptance:
        self._lease.renew(now=now)
        if request.job_id != self._job_id:
            raise StateStoreError(
                "build-job-mismatch",
                f"build request targets job {request.job_id}, lane holds {self._job_id}",
            )
        snapshot = self._require_editorial_approved()
        if request.base_artifact_hash != snapshot.adopted_artifact_hash:
            raise StateStoreError(
                "build-base-stale",
                f"build request base {request.base_artifact_hash} is not the"
                f" adopted artifact {snapshot.adopted_artifact_hash}",
            )
        return BuildAcceptance(
            job_id=self._job_id,
            base_artifact_hash=request.base_artifact_hash,
            accepted_seq=snapshot.updated_at_seq,
        )

    def release(self, *, now: int) -> None:
        self._lease.release(now=now)

    def _require_editorial_approved(self) -> CasSnapshot:
        snapshot = current_job_state(self._store, self._job_id)
        if snapshot.status != "EDITORIAL_APPROVED":
            raise StateStoreError(
                "build-status",
                f"job {self._job_id} is {snapshot.status}; Resolve builds run"
                " only from EDITORIAL_APPROVED",
            )
        return snapshot


__all__ = [
    "BuildAcceptance", "BuildRequest", "PlanCommitLane",
    "PlanCommitOutcome", "ResolveBuildLane", "StateLane",
    "plan_commit_resource", "resolve_build_resource", "state_resource",
]
