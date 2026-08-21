"""Blocker-fix regression: StageRunner must not adopt over a stale parent.

The old commit path re-read the LATEST job state and self-transitioned
against it, so a slow stage whose parent had been superseded mid-run by
another writer silently adopted its output over the newer parent. The
fixed runner captures the starting status + adopted hash at acquire
time, CASes against those exact expectations, and reports a typed
``superseded`` outcome WITHOUT adoption on drift.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.store import ArtifactStore
from services.contracts.primitives import Producer
from services.job_runner.cas import current_job_state
from services.job_runner.lanes import StateLane
from services.job_runner.stage_runner import StageRunner
from services.job_runner.stage_runner_models import (
    RetryPolicy,
    SequenceClock,
)
from services.job_runner.state_store import StateStore

JOB = "job-1"
STAGE = "normalize"
POLICY = RetryPolicy(max_attempts=3)


def synthetic_hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _supersede_parent(state: StateStore, parent_hash: str) -> None:
    snapshot = current_job_state(state, JOB)
    lane = StateLane(state, JOB, holder="parent-b")
    lane.acquire(now=5, ttl_seconds=60)
    try:
        lane.apply(
            expected_status=snapshot.status,
            expected_parent_hash=snapshot.adopted_artifact_hash,
            new_status=snapshot.status,
            new_artifact_hash=parent_hash,
            now=5,
        )
    finally:
        lane.release(now=5)


def test_stale_stage_output_is_superseded_not_adopted(tmp_path: Path) -> None:
    state = StateStore.open(tmp_path / "state.sqlite3")
    state.create_job(job_id=JOB, episode_id="ep-1", current_stage=STAGE)
    artifact_store = ArtifactStore(tmp_path / "artifact-store")
    registry = ArtifactRegistry(tmp_path / "registry")
    parent_b_hash = synthetic_hash("parent-b-output")

    def superseding_hook(content_hash: str) -> None:
        del content_hash
        _supersede_parent(state, parent_b_hash)

    runner = StageRunner(
        artifact_store=artifact_store,
        journal_root=tmp_path / "stage-journal",
        runner_version="stage-v1",
        code_snapshot_id="snap-1",
        producer=Producer(name="stage-runner-test", version="t1"),
        before_record_hook=superseding_hook,
    )
    result = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        _ok_fn(b"stale-stage-output"), POLICY, SequenceClock(),
    )

    assert result.outcome == "superseded"
    assert result.output_artifact_hash is not None
    snapshot = current_job_state(state, JOB)
    assert snapshot.adopted_artifact_hash == parent_b_hash

    reused = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        _unreachable_fn(), POLICY, SequenceClock(),
    )
    assert reused.outcome == "reused"
    assert reused.output_artifact_hash == result.output_artifact_hash
    assert current_job_state(state, JOB).adopted_artifact_hash == parent_b_hash


def test_fresh_parent_adoption_still_succeeds(tmp_path: Path) -> None:
    state = StateStore.open(tmp_path / "state.sqlite3")
    state.create_job(job_id=JOB, episode_id="ep-1", current_stage=STAGE)
    artifact_store = ArtifactStore(tmp_path / "artifact-store")
    registry = ArtifactRegistry(tmp_path / "registry")
    runner = StageRunner(
        artifact_store=artifact_store,
        journal_root=tmp_path / "stage-journal",
        runner_version="stage-v1",
        code_snapshot_id="snap-1",
        producer=Producer(name="stage-runner-test", version="t1"),
    )
    result = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        _ok_fn(b"fresh-output"), POLICY, SequenceClock(),
    )
    assert result.outcome == "succeeded"
    snapshot = current_job_state(state, JOB)
    assert snapshot.adopted_artifact_hash == result.output_artifact_hash


def _ok_fn(payload: bytes) -> Callable[[int], bytes]:
    def runner(attempt: int) -> bytes:
        del attempt
        return payload

    return runner


def _unreachable_fn() -> Callable[[int], bytes]:
    def runner(attempt: int) -> bytes:
        raise AssertionError(f"runner_fn must not re-execute (attempt {attempt})")

    return runner
