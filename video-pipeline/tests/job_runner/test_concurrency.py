"""Serialized-authority concurrency tests (Todo 10).

Races are exercised deterministically: the CAS re-reads current state
inside its transaction, so the interleaving that matters (a second
writer holding a stale expectation) is simulated by sequencing — the
loser must observe ``superseded`` and write nothing. Leases use the
Todo-9 logical clock; no test sleeps.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from services.job_runner.cas import (
    ApprovalRef,
    CasError,
    TransitionPayload,
    apply_transition,
    current_job_state,
)
from services.job_runner.lanes import (
    BuildRequest,
    PlanCommitLane,
    ResolveBuildLane,
    StateLane,
    plan_commit_resource,
    resolve_build_resource,
)
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_store import StateStore
from services.job_runner.transitions import (
    APPROVAL_PURPOSE_EDITORIAL,
    APPROVAL_PURPOSE_FINAL,
    MAIN_PATH,
)
from services.review_command.commit import CommitOutcome
from services.review_command.store import OperatorDecision0C, initialize_store, load_index
from tests.review_command.support import fixture_proposal, manifest_plan

if TYPE_CHECKING:
    from services.job_runner.cas import CasSnapshot
    from services.review_command.models import ReviewCommandProposal0C

JOB = "job-1"
GATED = {"EDITORIAL_APPROVED": APPROVAL_PURPOSE_EDITORIAL, "FINAL_APPROVED": APPROVAL_PURPOSE_FINAL}


def synthetic_hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def opened(tmp_path: Path) -> StateStore:
    store = StateStore.open(tmp_path / "state.sqlite3")
    store.create_job(job_id=JOB, episode_id="ep-1", current_stage="ingest")
    return store


def editorial_payload(parent: str) -> TransitionPayload:
    return TransitionPayload(
        approval=ApprovalRef(
            purpose=APPROVAL_PURPOSE_EDITORIAL,
            target_hash=parent,
            artifact_ref="operator-checkpoint",
        )
    )


def gated_payload(target: str, parent: str) -> TransitionPayload | None:
    if target not in GATED:
        return None
    return TransitionPayload(
        approval=ApprovalRef(
            purpose=GATED[target],
            target_hash=parent,
            artifact_ref=f"operator-checkpoint-{GATED[target]}",
        )
    )


def advance(store: StateStore, *, to_status: str) -> CasSnapshot:
    snapshot = current_job_state(store, JOB)
    target_index = MAIN_PATH.index(cast("str", to_status))
    if MAIN_PATH.index(snapshot.status) == target_index:
        return snapshot
    for target in MAIN_PATH[MAIN_PATH.index(snapshot.status) + 1 : target_index + 1]:
        payload = gated_payload(str(target), snapshot.adopted_artifact_hash or "")
        snapshot = apply_transition(
            store,
            JOB,
            expected_status=snapshot.status,
            expected_parent_hash=snapshot.adopted_artifact_hash,
            new_status=target,
            new_artifact_hash=synthetic_hash(f"step-{target}"),
            payload=payload,
        )
    return snapshot


def test_single_writer_completes_legal_path(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        lane = StateLane(store, JOB, holder="writer-a")
        lane.acquire(now=100, ttl_seconds=500)
        snapshot = current_job_state(store, JOB)
        for target in MAIN_PATH[1:]:
            payload = gated_payload(str(target), snapshot.adopted_artifact_hash or "")
            snapshot = lane.apply(
                expected_status=snapshot.status,
                expected_parent_hash=snapshot.adopted_artifact_hash,
                new_status=target,
                new_artifact_hash=synthetic_hash(f"walk-{target}"),
                payload=payload,
                now=110,
            )
        lane.release(now=120)

        final = current_job_state(store, JOB)
        assert final.status == "FROZEN"
        assert final.adopted_artifact_hash == synthetic_hash("walk-FROZEN")
        # Lease released: a second writer acquires the same resource.
        StateLane(store, JOB, holder="writer-b").acquire(now=130, ttl_seconds=10)


def test_two_writers_race_same_transition_one_superseded(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        before = current_job_state(store, JOB)

        committed = apply_transition(
            store,
            JOB,
            expected_status=before.status,
            expected_parent_hash=before.adopted_artifact_hash,
            new_status="INGESTED",
            new_artifact_hash=synthetic_hash("writer-a-ingest"),
        )
        with pytest.raises(CasError, match="superseded") as error:
            apply_transition(
                store,
                JOB,
                expected_status=before.status,
                expected_parent_hash=before.adopted_artifact_hash,
                new_status="INGESTED",
                new_artifact_hash=synthetic_hash("writer-b-ingest"),
            )
        assert error.value.code == "superseded"

        after = current_job_state(store, JOB)
        assert after == committed
        assert after.adopted_artifact_hash == synthetic_hash("writer-a-ingest")
        # Exactly one row write: the sequence advanced once, one adoption.
        assert after.updated_at_seq == before.updated_at_seq + 1


def test_stale_parent_hash_superseded(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        first = apply_transition(
            store,
            JOB,
            expected_status="CREATED",
            expected_parent_hash=None,
            new_status="INGESTED",
            new_artifact_hash=synthetic_hash("ingest"),
        )
        second = apply_transition(
            store,
            JOB,
            expected_status="INGESTED",
            expected_parent_hash=synthetic_hash("ingest"),
            new_status="NORMALIZED",
            new_artifact_hash=synthetic_hash("normalize"),
        )

        with pytest.raises(CasError, match="superseded"):
            apply_transition(
                store,
                JOB,
                expected_status="INGESTED",
                expected_parent_hash=first.adopted_artifact_hash,
                new_status="NORMALIZED",
                new_artifact_hash=synthetic_hash("stale-normalize"),
            )
        assert current_job_state(store, JOB) == second


def test_expired_lease_late_writer_superseded(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        stale_lane = StateLane(store, JOB, holder="writer-a")
        stale_lane.acquire(now=100, ttl_seconds=50)  # expires at 150

        fresh_lane = StateLane(store, JOB, holder="writer-b")
        fresh_lane.acquire(now=200, ttl_seconds=50)
        committed = fresh_lane.apply(
            expected_status="CREATED",
            expected_parent_hash=None,
            new_status="INGESTED",
            new_artifact_hash=synthetic_hash("writer-b-ingest"),
            now=210,
        )
        fresh_lane.release(now=220)

        # The expired holder cannot renew through the lane (lease stolen).
        with pytest.raises(StateStoreError) as renew_error:
            stale_lane.apply(
                expected_status="CREATED",
                expected_parent_hash=None,
                new_status="INGESTED",
                new_artifact_hash=synthetic_hash("writer-a-ingest"),
                now=230,
            )
        assert renew_error.value.code in {"not-holder", "lease-expired", "lease-missing"}

        # Even a rogue late CAS bypassing lane discipline is superseded.
        with pytest.raises(CasError, match="superseded"):
            apply_transition(
                store,
                JOB,
                expected_status="CREATED",
                expected_parent_hash=None,
                new_status="INGESTED",
                new_artifact_hash=synthetic_hash("writer-a-ingest"),
            )
        assert current_job_state(store, JOB) == committed


def test_lane_exclusion_second_state_acquisition_refused(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        first = StateLane(store, JOB, holder="writer-a")
        first.acquire(now=10, ttl_seconds=100)

        with pytest.raises(StateStoreError, match="lease-held"):
            StateLane(store, JOB, holder="writer-b").acquire(now=20, ttl_seconds=100)

        first.release(now=30)
        StateLane(store, JOB, holder="writer-b").acquire(now=40, ttl_seconds=100)


def plan_store(tmp_path: Path) -> tuple[Path, Path, str]:
    plan_dir = tmp_path / "plan-store"
    plan_dir.mkdir()
    log_path = plan_dir / "events.jsonl"
    base = manifest_plan("p0c-span-clear")
    initialize_store(base, log_path, plan_dir)
    v1_hash = load_index(plan_dir).versions["1"].plan_sha256
    assert isinstance(v1_hash, str)
    return log_path, plan_dir, v1_hash


def plan_commit_lane(store: StateStore, holder: str) -> PlanCommitLane:
    return PlanCommitLane(store, JOB, holder=holder)


def seeded_plan_job(tmp_path: Path, store: StateStore) -> tuple[Path, Path]:
    log_path, plan_dir, v1_hash = plan_store(tmp_path)
    advance(store, to_status="PLAN_COMMITTED")
    apply_transition(
        store,
        JOB,
        expected_status="PLAN_COMMITTED",
        expected_parent_hash=current_job_state(store, JOB).adopted_artifact_hash,
        new_status="PLAN_COMMITTED",
        new_artifact_hash=v1_hash,
    )
    return log_path, plan_dir


def test_plan_commit_lane_records_state_adoption(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        log_path, plan_dir = seeded_plan_job(tmp_path, store)
        before = current_job_state(store, JOB)

        outcome = plan_commit_lane(store, "commit-a").commit(
            proposal=fixture_proposal("p0c-span-clear"),
            decision=OperatorDecision0C(decision_id="dec-001", actor_intent="operator"),
            events_log_path=log_path,
            plan_dir=plan_dir,
            now=10,
            ttl_seconds=100,
        )

        assert isinstance(outcome.commit, CommitOutcome)
        assert (outcome.commit.version, outcome.commit.deferred, outcome.commit.idempotent) == (
            2,
            False,
            False,
        )
        adopted_hash = load_index(plan_dir).versions["2"].plan_sha256
        assert adopted_hash != before.adopted_artifact_hash
        assert outcome.state is not None
        assert outcome.state.adopted_artifact_hash == adopted_hash

        after = current_job_state(store, JOB)
        assert after.status == "PLAN_COMMITTED"
        assert after.adopted_artifact_hash == adopted_hash
        assert after.updated_at_seq == before.updated_at_seq + 1
        # Both the plan-commit lease and the inner state lease are released.
        StateLane(store, JOB, holder="probe").acquire(now=20, ttl_seconds=10)
        store.acquire_lease(
            resource=plan_commit_resource(JOB), holder="probe", now=25, ttl_seconds=10
        )


def test_plan_commit_second_concurrent_same_base(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        log_path, plan_dir = seeded_plan_job(tmp_path, store)
        first = plan_commit_lane(store, "commit-a")
        first.acquire(now=10, ttl_seconds=100)

        with pytest.raises(StateStoreError, match="lease-held"):
            plan_commit_lane(store, "commit-b").commit(
                proposal=fixture_proposal("p0c-span-clear"),
                decision=OperatorDecision0C(decision_id="dec-002", actor_intent="operator"),
                events_log_path=log_path,
                plan_dir=plan_dir,
                now=20,
                ttl_seconds=100,
            )

        # A commits under its lease (same-holder re-acquire is a refresh).
        first_commit = first.commit(
            proposal=fixture_proposal("p0c-span-clear"),
            decision=OperatorDecision0C(decision_id="dec-001", actor_intent="operator"),
            events_log_path=log_path,
            plan_dir=plan_dir,
            now=25,
            ttl_seconds=100,
        )
        adopted = current_job_state(store, JOB)
        assert first_commit.state is not None

        # The same-base duplicate replays idempotently and adopts nothing new.
        replay = plan_commit_lane(store, "commit-b").commit(
            proposal=fixture_proposal("p0c-span-clear"),
            decision=OperatorDecision0C(decision_id="dec-003", actor_intent="operator"),
            events_log_path=log_path,
            plan_dir=plan_dir,
            now=40,
            ttl_seconds=100,
        )
        assert replay.commit.idempotent is True
        assert replay.state is None
        assert current_job_state(store, JOB).adopted_artifact_hash == (
            load_index(plan_dir).versions["2"].plan_sha256
        )
        assert current_job_state(store, JOB) == adopted


def test_resolve_build_lane_gates_and_lease(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        advance(store, to_status="PREVIEW_READY")
        parent = current_job_state(store, JOB).adopted_artifact_hash
        assert parent is not None

        with pytest.raises(StateStoreError, match="build-status") as gate:
            ResolveBuildLane(store, JOB, holder="builder-a").open(now=10, ttl_seconds=100)
        assert gate.value.code == "build-status"
        # Refused open releases the lease: the resource is acquirable again.
        store.acquire_lease(
            resource=resolve_build_resource(JOB), holder="probe", now=15, ttl_seconds=5
        )

        advance(store, to_status="EDITORIAL_APPROVED")
        approved = current_job_state(store, JOB).adopted_artifact_hash
        assert approved is not None
        lane = ResolveBuildLane(store, JOB, holder="builder-a").open(now=30, ttl_seconds=100)
        with pytest.raises(StateStoreError, match="lease-held"):
            ResolveBuildLane(store, JOB, holder="builder-b").open(now=35, ttl_seconds=100)

        request = BuildRequest(
            job_id=JOB,
            base_artifact_hash=approved,
            requested_by="resolve-builder",
        )
        acceptance = lane.accept(request, now=40)
        assert (acceptance.job_id, acceptance.base_artifact_hash) == (JOB, approved)

        stale = BuildRequest(
            job_id=JOB,
            base_artifact_hash=parent,
            requested_by="resolve-builder",
        )
        with pytest.raises(StateStoreError, match="build-base"):
            lane.accept(stale, now=45)

        lane.release(now=50)
        ResolveBuildLane(store, JOB, holder="builder-b").open(now=55, ttl_seconds=10).release(
            now=60
        )


def test_crash_between_plan_write_and_state_cas_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with opened(tmp_path) as store:
        log_path, plan_dir = seeded_plan_job(tmp_path, store)
        before = current_job_state(store, JOB)
        proposal: ReviewCommandProposal0C = fixture_proposal("p0c-span-clear")
        decision = OperatorDecision0C(decision_id="dec-001", actor_intent="operator")

        def crash(*args: object, **kwargs: object) -> object:
            raise RuntimeError("simulated crash after plan write, before state CAS")

        monkeypatch.setattr("services.job_runner.lanes.apply_transition", crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            plan_commit_lane(store, "commit-a").commit(
                proposal=proposal,
                decision=decision,
                events_log_path=log_path,
                plan_dir=plan_dir,
                now=10,
                ttl_seconds=100,
            )
        monkeypatch.undo()

        # Orphan plan is detectable via the versions index; state NOT advanced.
        index = load_index(plan_dir)
        assert "2" in index.versions
        assert (plan_dir / "plan-v2.json").is_file()
        assert current_job_state(store, JOB) == before

        recovered = plan_commit_lane(store, "commit-b").commit(
            proposal=proposal,
            decision=decision,
            events_log_path=log_path,
            plan_dir=plan_dir,
            now=30,
            ttl_seconds=100,
        )
        assert recovered.commit.idempotent is True
        assert recovered.state is not None
        assert recovered.state.adopted_artifact_hash == index.versions["2"].plan_sha256
        assert current_job_state(store, JOB).adopted_artifact_hash == (
            index.versions["2"].plan_sha256
        )
