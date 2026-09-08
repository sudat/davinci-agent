from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.artifact_registry.store_view import object_path
from services.job_runner.state_integrity import recover_job, verify_against_store
from services.job_runner.state_models import CachePointerRow, JobRow, LeaseRow
from services.job_runner.state_store import StateStore, StateStoreError
from tests.job_runner.support import (
    make_artifact_store,
    make_registry,
    make_stage_run,
    open_raw,
    publish,
    publish_and_register,
    sha256_bytes,
    table_columns,
)

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore


def opened(tmp_path: Path, name: str = "state.sqlite3") -> StateStore:
    return StateStore.open(tmp_path / name)


def seeded_job(store: StateStore) -> JobRow:
    return store.create_job(
        job_id="job-1",
        episode_id="ep-1",
        current_stage="ingest",
    )


def test_create_job_and_snapshot_roundtrip(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        created = seeded_job(store)

        assert created.status == "CREATED"
        assert created.created_at_seq == created.updated_at_seq

        snapshot = store.get_job_snapshot("job-1")
        assert snapshot.job == created

        with pytest.raises(StateStoreError, match="job-exists") as error:
            seeded_job(store)
        assert error.value.code == "job-exists"

        with pytest.raises(StateStoreError, match="job-missing"):
            store.get_job_snapshot("job-none")


def test_record_stage_run_idempotent_by_key_and_hash(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        seeded_job(store)
        adopted = sha256_bytes(b"adopted-output")

        first = store.record_stage_run(
            make_stage_run(stage_name="ingest", idempotency_key="key-1")
        )
        pending = first.model_copy(
            update={"status": "running", "adopted_artifact_hash": adopted}
        )
        adopted_row = store.record_stage_run(pending)

        assert adopted_row.adopted_artifact_hash == adopted
        assert adopted_row.status == "running"

        replay = store.record_stage_run(adopted_row)
        assert replay == adopted_row

        snapshot = store.get_job_snapshot("job-1")
        assert len(snapshot.stage_runs) == 1


def test_record_stage_run_conflicts_on_key_hash_disagreement(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        seeded_job(store)
        adopted = sha256_bytes(b"adopted-output")
        row = store.record_stage_run(
            make_stage_run(
                stage_name="ingest",
                idempotency_key="key-1",
                adopted_artifact_hash=adopted,
            )
        )

        other_hash = sha256_bytes(b"other-output")
        with pytest.raises(StateStoreError, match="stage-run-conflict") as error:
            store.record_stage_run(
                row.model_copy(update={"adopted_artifact_hash": other_hash})
            )
        assert error.value.code == "stage-run-conflict"

        with pytest.raises(StateStoreError, match="stage-run-conflict"):
            store.record_stage_run(
                row.model_copy(update={"adopted_artifact_hash": None})
            )

        with pytest.raises(StateStoreError, match="stage-run-conflict"):
            store.record_stage_run(
                row.model_copy(
                    update={"input_artifact_hashes": (sha256_bytes(b"other-input"),)}
                )
            )


def test_record_stage_run_requires_existing_job(tmp_path: Path) -> None:
    with opened(tmp_path) as store, pytest.raises(
        StateStoreError, match="job-missing"
    ):
        store.record_stage_run(
            make_stage_run(stage_name="ingest", idempotency_key="key-1")
        )


def test_bump_retry_and_set_stage_status(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        seeded_job(store)
        store.record_stage_run(
            make_stage_run(
                stage_name="normalize",
                idempotency_key="key-1",
                status="pending",
            )
        )

        running = store.set_stage_status(
            job_id="job-1", idempotency_key="key-1", status="running"
        )
        assert running.status == "running"

        first = store.bump_retry(
            job_id="job-1", idempotency_key="key-1", error_code="timeout"
        )
        second = store.bump_retry(
            job_id="job-1", idempotency_key="key-1", error_code="resolve-disconnect"
        )
        assert first.retry_count == 1
        assert second.retry_count == 2
        assert second.last_error_code == "resolve-disconnect"

        failed = store.set_stage_status(
            job_id="job-1",
            idempotency_key="key-1",
            status="failed_retryable",
            last_error_code="resolve-disconnect",
        )
        assert failed.status == "failed_retryable"

        with pytest.raises(StateStoreError, match="stage-run-missing"):
            store.bump_retry(
                job_id="job-1", idempotency_key="key-none", error_code="timeout"
            )
        with pytest.raises(StateStoreError, match="stage-run-missing"):
            store.set_stage_status(
                job_id="job-1", idempotency_key="key-none", status="running"
            )


def test_lease_acquire_expire_renew_release(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        lease = store.acquire_lease(
            resource="resolve-builder", holder="writer-a", now=1_000, ttl_seconds=60
        )
        assert lease == LeaseRow(
            resource="resolve-builder", holder="writer-a", expires_at=1_060
        )

        with pytest.raises(StateStoreError, match="lease-held"):
            store.acquire_lease(
                resource="resolve-builder", holder="writer-b", now=1_059, ttl_seconds=60
            )

        renewed = store.renew_lease(
            resource="resolve-builder", holder="writer-a", now=1_030, ttl_seconds=60
        )
        assert renewed.expires_at == 1_090

        with pytest.raises(StateStoreError, match="not-holder"):
            store.renew_lease(
                resource="resolve-builder", holder="writer-b", now=1_050, ttl_seconds=60
            )

        stolen = store.acquire_lease(
            resource="resolve-builder", holder="writer-b", now=1_091, ttl_seconds=60
        )
        assert stolen.holder == "writer-b"

        with pytest.raises(StateStoreError, match="lease-expired"):
            store.renew_lease(
                resource="resolve-builder", holder="writer-b", now=1_160, ttl_seconds=60
            )

        store.release_lease(resource="resolve-builder", holder="writer-b", now=1_120)

        with pytest.raises(StateStoreError, match="lease-missing"):
            store.renew_lease(
                resource="resolve-builder", holder="writer-b", now=1_180, ttl_seconds=60
            )
        with pytest.raises(StateStoreError, match="lease-missing"):
            store.release_lease(
                resource="resolve-builder", holder="writer-b", now=1_180
            )


def test_lease_release_requires_holder_and_fresh_lease(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        store.acquire_lease(
            resource="job-state", holder="writer-a", now=1_000, ttl_seconds=60
        )

        with pytest.raises(StateStoreError, match="not-holder"):
            store.release_lease(resource="job-state", holder="writer-b", now=1_010)

        with pytest.raises(StateStoreError, match="lease-expired"):
            store.release_lease(resource="job-state", holder="writer-a", now=1_100)


def test_approval_ref_roundtrip_and_idempotency(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        target = sha256_bytes(b"approval-target")

        recorded = store.record_approval_ref(
            purpose="editorial", target_hash=target, artifact_ref="artifact-1"
        )
        replayed = store.record_approval_ref(
            purpose="editorial", target_hash=target, artifact_ref="artifact-1"
        )

        assert recorded.recorded_seq >= 1
        assert replayed == recorded

        refs = store.get_approval_refs()
        assert len(refs) == 1
        assert refs[0].purpose == "editorial"
        assert refs[0].target_hash == target


def test_cache_pointer_roundtrip(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        pointer = CachePointerRow(
            cache_key="analysis-frames:ep-1",
            artifact_hash=sha256_bytes(b"cache-payload"),
            producer_version="analyzer-v3",
        )
        store.put_cache_pointer(pointer)
        assert store.get_cache_pointer("analysis-frames:ep-1") == pointer

        replaced = pointer.model_copy(update={"producer_version": "analyzer-v4"})
        store.put_cache_pointer(replaced)
        assert store.get_cache_pointer("analysis-frames:ep-1") == replaced

        assert store.get_cache_pointer("cache-none") is None
        assert store.get_cache_pointers() == (replaced,)


def test_migrate_and_recover_job(tmp_path: Path) -> None:
    ingest_input = sha256_bytes(b"camera-original")
    ingest_output = sha256_bytes(b"source-manifest")

    with opened(tmp_path) as store:
        seeded_job(store)
        store.record_stage_run(
            make_stage_run(
                stage_name="ingest",
                idempotency_key="key-ingest",
                input_artifact_hashes=(ingest_input,),
                adopted_artifact_hash=ingest_output,
            )
        )
        store.record_stage_run(
            make_stage_run(
                stage_name="normalize",
                idempotency_key="key-normalize",
                input_artifact_hashes=(ingest_output,),
                status="running",
            )
        )

    with opened(tmp_path) as store:
        recovery = recover_job(store, "job-1")

    assert recovery.job_id == "job-1"
    assert recovery.job_status == "CREATED"
    assert recovery.current_stage == "ingest"
    assert recovery.last_succeeded_stage == "ingest"
    assert recovery.resume_input_hashes == (ingest_input,)
    assert recovery.last_adopted_hash == ingest_output


def test_recover_job_without_committed_runs(tmp_path: Path) -> None:
    with opened(tmp_path) as store:
        seeded_job(store)
        store.record_stage_run(
            make_stage_run(
                stage_name="ingest",
                idempotency_key="key-ingest",
                status="running",
            )
        )

        recovery = recover_job(store, "job-1")

        assert recovery.last_succeeded_stage is None
        assert recovery.resume_input_hashes == ()
        assert recovery.last_adopted_hash is None

        with pytest.raises(StateStoreError, match="job-missing"):
            recover_job(store, "job-none")


def test_adopted_hash_roundtrip_verify_passes(tmp_path: Path) -> None:
    artifact_store = make_artifact_store(tmp_path)
    registry = make_registry(tmp_path)
    input_receipt = publish_and_register(
        artifact_store, registry, "camera-original", b"camera-original-bytes"
    )
    output_receipt = publish_and_register(
        artifact_store, registry, "source-manifest", b"source-manifest-bytes"
    )

    with opened(tmp_path) as store:
        seeded_job(store)
        store.record_stage_run(
            make_stage_run(
                stage_name="ingest",
                idempotency_key="key-ingest",
                input_artifact_hashes=(input_receipt.content_sha256,),
                adopted_artifact_hash=output_receipt.content_sha256,
            )
        )
        store.record_approval_ref(
            purpose="editorial",
            target_hash=output_receipt.content_sha256,
            artifact_ref=output_receipt.artifact_id,
        )
        store.put_cache_pointer(
            CachePointerRow(
                cache_key="analysis-frames:ep-1",
                artifact_hash=input_receipt.content_sha256,
                producer_version="analyzer-v3",
            )
        )

        report = verify_against_store(store, artifact_store, registry)

    assert report.checked_stage_runs == 1
    assert report.checked_hashes == 2
    assert report.checked_approval_refs == 1
    assert report.checked_cache_pointers == 1


def test_verify_missing_artifact_hash_fails_closed(tmp_path: Path) -> None:
    artifact_store = make_artifact_store(tmp_path)
    registry = make_registry(tmp_path)

    with opened(tmp_path) as store:
        seeded_job(store)
        store.record_stage_run(
            make_stage_run(
                stage_name="ingest",
                idempotency_key="key-ingest",
                adopted_artifact_hash=sha256_bytes(b"never-published"),
            )
        )

        with pytest.raises(StateStoreError) as error:
            verify_against_store(store, artifact_store, registry)
    assert error.value.code == "missing-artifact"


def test_verify_unregistered_hash_fails_closed(tmp_path: Path) -> None:
    artifact_store = make_artifact_store(tmp_path)
    registry = make_registry(tmp_path)
    receipt = publish(artifact_store, "unregistered-artifact", b"payload")

    with opened(tmp_path) as store:
        seeded_job(store)
        store.record_stage_run(
            make_stage_run(
                stage_name="ingest",
                idempotency_key="key-ingest",
                adopted_artifact_hash=receipt.content_sha256,
            )
        )

        with pytest.raises(StateStoreError) as error:
            verify_against_store(store, artifact_store, registry)
    assert error.value.code == "missing-artifact"


def test_verify_row_hash_disagreement_fails_closed(tmp_path: Path) -> None:
    artifact_store = make_artifact_store(tmp_path)
    registry = make_registry(tmp_path)
    approved = publish_and_register(
        artifact_store, registry, "edit-plan", b"edit-plan-bytes"
    )
    other = publish_and_register(
        artifact_store, registry, "selection-plan", b"selection-plan-bytes"
    )

    with opened(tmp_path) as store:
        seeded_job(store)
        store.record_approval_ref(
            purpose="editorial",
            target_hash=other.content_sha256,
            artifact_ref=approved.artifact_id,
        )

        with pytest.raises(StateStoreError) as error:
            verify_against_store(store, artifact_store, registry)
    assert error.value.code == "row-hash-disagreement"


def test_verify_recomputes_hashes_from_store_bytes(tmp_path: Path) -> None:
    artifact_store: ArtifactStore = make_artifact_store(tmp_path)
    registry: ArtifactRegistry = make_registry(tmp_path)
    receipt = publish_and_register(
        artifact_store, registry, "source-manifest", b"original-bytes"
    )

    with opened(tmp_path) as store:
        seeded_job(store)
        store.record_stage_run(
            make_stage_run(
                stage_name="ingest",
                idempotency_key="key-ingest",
                adopted_artifact_hash=receipt.content_sha256,
            )
        )

        object_path(artifact_store.store_root, receipt.content_sha256).write_bytes(
            b"tampered-bytes"
        )

        with pytest.raises(StateStoreError) as error:
            verify_against_store(store, artifact_store, registry)
    assert error.value.code == "row-hash-disagreement"


def test_verify_detects_duplicate_key_hash_rows_in_tampered_db(tmp_path: Path) -> None:
    artifact_store = make_artifact_store(tmp_path)
    registry = make_registry(tmp_path)
    first = publish_and_register(artifact_store, registry, "output-a", b"output-a")
    second = publish_and_register(artifact_store, registry, "output-b", b"output-b")

    db_path = tmp_path / "state.sqlite3"
    with StateStore.open(db_path) as store:
        seeded_job(store)
        store.record_stage_run(
            make_stage_run(
                stage_name="ingest",
                idempotency_key="key-ingest",
                adopted_artifact_hash=first.content_sha256,
            )
        )

    tamper = open_raw(db_path)
    tamper.execute("ALTER TABLE stage_runs RENAME TO stage_runs_orig")
    tamper.execute(
        "CREATE TABLE stage_runs ("
        " job_id TEXT NOT NULL,"
        " stage_name TEXT NOT NULL,"
        " idempotency_key TEXT NOT NULL,"
        " input_artifact_hashes TEXT NOT NULL,"
        " adopted_artifact_hash TEXT,"
        " status TEXT NOT NULL,"
        " retry_count INTEGER NOT NULL,"
        " last_error_code TEXT,"
        " run_id TEXT,"
        " first_started_at TEXT,"
        " first_output_arrived_at TEXT,"
        " last_transition_at TEXT)"
    )
    tamper.execute("INSERT INTO stage_runs SELECT * FROM stage_runs_orig")
    tamper.execute("DROP TABLE stage_runs_orig")
    tamper.execute(
        "INSERT INTO stage_runs (job_id, stage_name, idempotency_key,"
        " input_artifact_hashes, adopted_artifact_hash, status, retry_count,"
        " last_error_code)"
        " VALUES ('job-1', 'ingest', 'key-ingest', '[]', ?, 'succeeded', 0, NULL)",
        (second.content_sha256,),
    )
    tamper.close()

    with (
        StateStore.open(db_path) as store,
        pytest.raises(StateStoreError) as error,
    ):
        verify_against_store(store, artifact_store, registry)
    assert error.value.code == "row-hash-disagreement"


def test_verify_missing_cache_pointer_target_fails_closed(tmp_path: Path) -> None:
    artifact_store = make_artifact_store(tmp_path)
    registry = make_registry(tmp_path)

    with opened(tmp_path) as store:
        seeded_job(store)
        store.put_cache_pointer(
            CachePointerRow(
                cache_key="analysis-frames:ep-1",
                artifact_hash=sha256_bytes(b"evicted-cache-object"),
                producer_version="analyzer-v3",
            )
        )

        with pytest.raises(StateStoreError) as error:
            verify_against_store(store, artifact_store, registry)
    assert error.value.code == "missing-artifact"


def test_schema_discipline_no_plan_body_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    StateStore.open(db_path).close()

    columns = table_columns(db_path)
    assert "stage_runs" in columns

    for table, table_column_names in columns.items():
        for column in table_column_names:
            lowered = column.lower()
            assert "body" not in lowered, (table, column)
            assert "plan" not in lowered, (table, column)
            assert "payload" not in lowered, (table, column)

    assert set(columns["stage_runs"]) == {
        "job_id",
        "stage_name",
        "idempotency_key",
        "input_artifact_hashes",
        "adopted_artifact_hash",
        "status",
        "retry_count",
        "last_error_code",
        "run_id",
        "first_started_at",
        "first_output_arrived_at",
        "last_transition_at",
    }
