"""Stage Runner idempotency, retry taxonomy, and recovery tests (Todo 11).

Every timing value is logical (``SequenceClock``); no test sleeps.
Crashes are simulated at the two fault seams — inside ``runner_fn``
(before publish) and the ``before_record_hook`` between publish and
the run record — so recovery is exercised without real process kills.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_registry.store_view import is_object_name, object_path
from services.artifact_store.store import ArtifactStore
from services.contracts.primitives import Producer
from services.job_runner.cas import current_job_state
from services.job_runner.stage_classify import classify_error
from services.job_runner.stage_runner import (
    StageRunJournal,
    StageRunner,
    stage_resource,
)
from services.job_runner.stage_runner_models import (
    RetryPolicy,
    SequenceClock,
    StageFnError,
    StageRunKey,
)
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_store import StateStore

if TYPE_CHECKING:
    from services.job_runner.stage_runner_models import StageRunResult
    from services.job_runner.state_models import StageRunRow

JOB = "job-1"
STAGE = "normalize"
POLICY = RetryPolicy(max_attempts=3, backoff_schedule=(1, 2))


class SimulatedCrashError(Exception):
    """Injected process-death simulation; never a classified failure."""


def synthetic_hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def key_for(*inputs: str, version: str = "stage-v1", snapshot: str = "snap-1") -> StageRunKey:
    return StageRunKey(
        stage_name=STAGE,
        input_hashes=tuple(synthetic_hash(item) for item in inputs),
        runner_version=version,
        code_snapshot_id=snapshot,
    )


def object_count(store_root: Path) -> int:
    objects = store_root / "objects"
    if not objects.is_dir():
        return 0
    return sum(1 for path in objects.rglob("*") if path.is_file() and is_object_name(path.name))


def setup(
    tmp_path: Path,
    *,
    runner_version: str = "stage-v1",
    code_snapshot_id: str = "snap-1",
    hook: Callable[[str], None] | None = None,
) -> tuple[StateStore, ArtifactRegistry, StageRunner, ArtifactStore]:
    state = StateStore.open(tmp_path / "state.sqlite3")
    state.create_job(job_id=JOB, episode_id="ep-1", current_stage=STAGE)
    artifact_store = ArtifactStore(tmp_path / "artifact-store")
    registry = ArtifactRegistry(tmp_path / "registry")
    runner = StageRunner(
        artifact_store=artifact_store,
        journal_root=tmp_path / "stage-journal",
        runner_version=runner_version,
        code_snapshot_id=code_snapshot_id,
        producer=Producer(name="stage-runner-test", version="t1"),
        before_record_hook=hook,
    )
    return state, registry, runner, artifact_store


def ok_fn(payload: bytes, calls: list[int] | None = None) -> Callable[[int], bytes]:
    def runner(attempt: int) -> bytes:
        if calls is not None:
            calls.append(attempt)
        return payload

    return runner


def flaky_fn(
    failures: int, code: str, payload: bytes, calls: list[int] | None = None
) -> Callable[[int], bytes]:
    def runner(attempt: int) -> bytes:
        if calls is not None:
            calls.append(attempt)
        if attempt <= failures:
            raise StageFnError(code, f"injected {code} on attempt {attempt}")
        return payload

    return runner


def always_fail_fn(code: str, calls: list[int]) -> Callable[[int], bytes]:
    def runner(attempt: int) -> bytes:
        calls.append(attempt)
        raise StageFnError(code, f"injected {code} on attempt {attempt}")

    return runner


def row_for(state: StateStore, idempotency_key: str) -> StageRunRow | None:
    for row in state.get_job_snapshot(JOB).stage_runs:
        if row.idempotency_key == idempotency_key:
            return row
    return None


def run_ok(result: StageRunResult) -> str:
    assert result.output_artifact_hash is not None
    return result.output_artifact_hash


def test_classify_error_is_a_pure_deterministic_table() -> None:
    assert classify_error("timeout") == "transient"
    assert classify_error("transport_reset") == "transient"
    assert classify_error("resolve_disconnect") == "transient"
    assert classify_error("schema_invalid") == "permanent"
    assert classify_error("missing_source") == "permanent"
    assert classify_error("capability_missing") == "permanent"
    assert classify_error("approval_required") == "blocking_human"
    assert classify_error("ambiguous_command") == "blocking_human"
    # Unknown codes are never retried (PRD 6: only known-transient codes retry).
    assert classify_error("mystery_fault") == "permanent"


def test_idempotency_key_covers_inputs_and_runner_identity() -> None:
    base = key_for("a", "b")
    same = StageRunKey(
        stage_name=STAGE,
        input_hashes=(synthetic_hash("b"), synthetic_hash("a")),  # order-insensitive
        runner_version="stage-v1",
        code_snapshot_id="snap-1",
    )
    changed_input = key_for("c")
    changed_version = key_for("a", version="stage-v2")
    changed_snapshot = key_for("a", snapshot="snap-2")
    changed_stage = base.model_copy(update={"stage_name": "analyze"})

    assert base.idempotency_key == same.idempotency_key
    for other in (changed_input, changed_version, changed_snapshot, changed_stage):
        assert other.idempotency_key != base.idempotency_key


def test_same_key_reuse_returns_first_hash_without_reexecution(tmp_path: Path) -> None:
    state, registry, runner, artifact_store = setup(tmp_path)
    calls: list[int] = []
    payload = b"normalize-output-v1"

    first = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        ok_fn(payload, calls), POLICY, SequenceClock(1000),
    )
    assert first.outcome == "succeeded"
    first_hash = run_ok(first)
    assert calls == [1]
    assert current_job_state(state, JOB).adopted_artifact_hash == first_hash

    second = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        ok_fn(b"different-bytes-never-executed", calls), POLICY, SequenceClock(2000),
    )
    assert second.outcome == "reused"
    assert second.attempts == 0
    assert second.output_artifact_hash == first_hash
    assert calls == [1]  # runner_fn never re-executed
    assert object_count(artifact_store.store_root) == 1  # exactly one committed output

    rows = state.get_job_snapshot(JOB).stage_runs
    assert len(rows) == 1
    assert rows[0].status == "succeeded"
    assert rows[0].adopted_artifact_hash == first_hash


def test_changed_input_hash_reruns_under_new_key(tmp_path: Path) -> None:
    state, registry, runner, artifact_store = setup(tmp_path)
    first = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        ok_fn(b"output-a"), POLICY, SequenceClock(),
    )
    second = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-b"),),
        ok_fn(b"output-b"), POLICY, SequenceClock(),
    )

    assert first.outcome == "succeeded"
    assert second.outcome == "succeeded"
    assert first.output_artifact_hash != second.output_artifact_hash
    assert len(state.get_job_snapshot(JOB).stage_runs) == 2
    assert object_count(artifact_store.store_root) == 2


def test_transient_faults_attempts_1_2_then_success_recovers_within_policy(
    tmp_path: Path,
) -> None:
    state, registry, runner, artifact_store = setup(tmp_path)
    calls: list[int] = []
    clock = SequenceClock(1000)

    result = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        flaky_fn(2, "timeout", b"recovered-output", calls), POLICY, clock,
    )

    assert result.outcome == "succeeded"
    assert result.attempts == 3
    assert calls == [1, 2, 3]
    # Logical backoff advanced per schedule: delay(1)=1 + delay(2)=2.
    assert clock.now == 1003
    row = row_for(state, key_for("input-a").idempotency_key)
    assert row is not None
    assert row.status == "succeeded"
    assert row.retry_count == 2
    assert row.adopted_artifact_hash == result.output_artifact_hash
    journal = StageRunJournal(tmp_path / "stage-journal", JOB).events()
    assert [event.kind for event in journal] == [
        "attempt-failed",
        "attempt-failed",
        "attempt-succeeded",
    ]
    assert all(event.identity.runner_version == "stage-v1" for event in journal)
    assert object_count(artifact_store.store_root) == 1


def test_resolve_disconnect_transient_recovers_within_policy(tmp_path: Path) -> None:
    state, registry, runner, _ = setup(tmp_path)
    calls: list[int] = []

    result = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        flaky_fn(1, "resolve_disconnect", b"post-reconnect-output", calls),
        POLICY, SequenceClock(5000),
    )

    assert result.outcome == "succeeded"
    assert result.attempts == 2
    assert calls == [1, 2]


def test_schema_invalid_permanent_blocks_without_retry(tmp_path: Path) -> None:
    state, registry, runner, artifact_store = setup(tmp_path)
    calls: list[int] = []

    result = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        always_fail_fn("schema_invalid", calls), POLICY, SequenceClock(),
    )

    assert result.outcome == "failed_blocked"
    assert result.attempts == 1  # NO retry for permanent failures
    assert result.failure_class == "permanent"
    assert result.error_code == "schema_invalid"
    assert result.needs_human is False
    assert result.output_artifact_hash is None
    assert calls == [1]
    row = row_for(state, key_for("input-a").idempotency_key)
    assert row is not None
    assert row.status == "failed_blocked"
    assert row.last_error_code == "schema_invalid"
    assert row.retry_count == 1
    assert object_count(artifact_store.store_root) == 0


def test_missing_source_permanent_blocks_without_retry(tmp_path: Path) -> None:
    state, registry, runner, _ = setup(tmp_path)
    calls: list[int] = []

    result = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        always_fail_fn("missing_source", calls), POLICY, SequenceClock(),
    )

    assert result.outcome == "failed_blocked"
    assert result.attempts == 1
    assert result.failure_class == "permanent"
    assert result.error_code == "missing_source"


def test_retry_exhaustion_blocks_after_exactly_max_attempts(tmp_path: Path) -> None:
    state, registry, runner, artifact_store = setup(tmp_path)
    calls: list[int] = []
    clock = SequenceClock(0)

    result = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        always_fail_fn("resolve_disconnect", calls), POLICY, clock,
    )

    assert result.outcome == "failed_blocked"
    assert result.attempts == 3  # exactly max_attempts, never an infinite loop
    assert calls == [1, 2, 3]
    assert result.failure_class == "transient"
    assert result.error_code == "resolve_disconnect"
    assert result.needs_human is False
    assert clock.now == 3  # bounded backoff 1 + 2, nothing more
    row = row_for(state, key_for("input-a").idempotency_key)
    assert row is not None
    assert row.status == "failed_blocked"
    assert row.retry_count == 3
    assert object_count(artifact_store.store_root) == 0
    journal = StageRunJournal(tmp_path / "stage-journal", JOB).events()
    assert [event.kind for event in journal] == [
        "attempt-failed",
        "attempt-failed",
        "attempt-failed",
        "run-blocked",
    ]


def test_approval_required_blocks_with_needs_human(tmp_path: Path) -> None:
    state, registry, runner, _ = setup(tmp_path)
    calls: list[int] = []

    result = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        always_fail_fn("approval_required", calls), POLICY, SequenceClock(),
    )

    assert result.outcome == "failed_blocked"
    assert result.attempts == 1
    assert result.failure_class == "blocking_human"
    assert result.needs_human is True
    journal = StageRunJournal(tmp_path / "stage-journal", JOB).events()
    blocked = journal[-1]
    assert blocked.kind == "run-blocked"
    assert blocked.failure_class == "blocking_human"
    assert blocked.error_code == "approval_required"


def test_crash_after_publish_before_record_readopts_existing_artifact(
    tmp_path: Path,
) -> None:
    def crash_hook(content_hash: str) -> None:
        raise SimulatedCrashError(f"kill after publish of {content_hash}")

    state, registry, runner, artifact_store = setup(tmp_path, hook=crash_hook)
    payload = b"published-but-unrecorded"
    with pytest.raises(SimulatedCrashError):
        runner.run(
            state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
            ok_fn(payload), POLICY, SequenceClock(),
        )

    # Crash window: artifact published, run record never completed.
    assert object_count(artifact_store.store_root) == 1
    key = key_for("input-a")
    crashed_row = row_for(state, key.idempotency_key)
    assert crashed_row is not None
    assert crashed_row.status != "succeeded"

    def must_not_execute(attempt: int) -> bytes:
        raise AssertionError("recovery must re-adopt without re-executing runner_fn")

    restarted = StageRunner(
        artifact_store=artifact_store,
        journal_root=tmp_path / "stage-journal",
        runner_version="stage-v1",
        code_snapshot_id="snap-1",
        producer=Producer(name="stage-runner-test", version="t1"),
    )
    result = restarted.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        must_not_execute, POLICY, SequenceClock(),
    )

    assert result.outcome == "recovered"
    assert result.attempts == 0
    assert result.output_artifact_hash == hashlib.sha256(payload).hexdigest()
    assert object_count(artifact_store.store_root) == 1  # no duplicate committed output
    adopted_row = row_for(state, key.idempotency_key)
    assert adopted_row is not None
    assert adopted_row.status == "succeeded"
    assert adopted_row.adopted_artifact_hash == result.output_artifact_hash
    assert current_job_state(state, JOB).adopted_artifact_hash == result.output_artifact_hash


def test_crash_before_publish_clean_rerun(tmp_path: Path) -> None:
    state, registry, runner, artifact_store = setup(tmp_path)
    calls: list[int] = []
    crashed_once = False

    def crash_once_fn(attempt: int) -> bytes:
        calls.append(attempt)
        nonlocal crashed_once
        if not crashed_once:
            crashed_once = True
            raise SimulatedCrashError("kill inside runner_fn before publish")
        return b"clean-rerun-output"

    with pytest.raises(SimulatedCrashError):
        runner.run(
            state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
            crash_once_fn, POLICY, SequenceClock(),
        )
    assert object_count(artifact_store.store_root) == 0

    result = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        crash_once_fn, POLICY, SequenceClock(),
    )

    assert result.outcome == "succeeded"
    assert calls == [1, 1]  # clean re-run restarted at attempt 1
    assert object_count(artifact_store.store_root) == 1


def test_runner_version_change_changes_key_and_reruns(tmp_path: Path) -> None:
    state, registry, runner_v1, artifact_store = setup(tmp_path, runner_version="stage-v1")
    first = runner_v1.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        ok_fn(b"v1-output"), POLICY, SequenceClock(),
    )
    assert first.outcome == "succeeded"

    runner_v2 = StageRunner(
        artifact_store=artifact_store,
        journal_root=tmp_path / "stage-journal",
        runner_version="stage-v2",
        code_snapshot_id="snap-1",
        producer=Producer(name="stage-runner-test", version="t1"),
    )
    second = runner_v2.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        ok_fn(b"v2-output"), POLICY, SequenceClock(),
    )

    assert second.outcome == "succeeded"  # rerun, NOT reuse
    assert second.output_artifact_hash != first.output_artifact_hash
    assert len(state.get_job_snapshot(JOB).stage_runs) == 2
    assert object_count(artifact_store.store_root) == 2


def test_reuse_fails_closed_when_recorded_hash_missing_from_store(
    tmp_path: Path,
) -> None:
    state, registry, runner, artifact_store = setup(tmp_path)
    first = runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        ok_fn(b"reuse-me"), POLICY, SequenceClock(),
    )
    recorded = run_ok(first)

    object_path(artifact_store.store_root, recorded).unlink()

    with pytest.raises(Exception, match="reuse-verify-failed"):
        runner.run(
            state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
            ok_fn(b"never-executed"), POLICY, SequenceClock(),
        )


def test_stage_lease_excludes_second_runner_during_execution(tmp_path: Path) -> None:
    state, registry, _runner, artifact_store = setup(tmp_path)
    probes: list[str] = []

    def probing_hook(content_hash: str) -> None:
        try:
            state.acquire_lease(
                resource=stage_resource(JOB, STAGE), holder="writer-b",
                now=10, ttl_seconds=5,
            )
        except StateStoreError as error:
            probes.append(error.code)
        raise SimulatedCrashError("end run inside the lease")

    runner_with_hook = StageRunner(
        artifact_store=artifact_store,
        journal_root=tmp_path / "stage-journal",
        runner_version="stage-v1",
        code_snapshot_id="snap-1",
        producer=Producer(name="stage-runner-test", version="t1"),
        before_record_hook=probing_hook,
    )
    with pytest.raises(SimulatedCrashError):
        runner_with_hook.run(
            state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
            ok_fn(b"leased-output"), POLICY, SequenceClock(),
        )

    assert probes == ["lease-held"]
    # The finally path released the stage lease after the crash propagated.
    state.acquire_lease(
        resource=stage_resource(JOB, STAGE), holder="writer-b", now=20, ttl_seconds=5
    )


def test_journal_is_hash_chained_and_tamper_evident(tmp_path: Path) -> None:
    state, registry, runner, _ = setup(tmp_path)
    runner.run(
        state, registry, JOB, STAGE, (synthetic_hash("input-a"),),
        flaky_fn(1, "timeout", b"journaled", []), POLICY, SequenceClock(),
    )
    journal_path = tmp_path / "stage-journal" / f"{JOB}-stage-runs.jsonl"
    assert journal_path.is_file()

    events = StageRunJournal(tmp_path / "stage-journal", JOB).events()
    assert [event.kind for event in events] == ["attempt-failed", "attempt-succeeded"]
    assert [event.sequence for event in events] == [1, 2]
    assert events[0].previous_event_hash == "0" * 64
    assert events[1].previous_event_hash == events[0].event_hash

    lines = journal_path.read_bytes().splitlines()
    tampered = lines[:]
    tampered[0] = tampered[0].replace(b'"attempt":1', b'"attempt":9')
    journal_path.write_bytes(b"\n".join(tampered) + b"\n")
    with pytest.raises(Exception, match="journal-chain"):
        StageRunJournal(tmp_path / "stage-journal", JOB).events()
