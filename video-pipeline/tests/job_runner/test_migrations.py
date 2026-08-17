from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from services.job_runner.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    MigrationError,
    apply_migrations,
)
from services.job_runner.state_store import StateStore
from tests.job_runner.support import open_raw, read_schema_version, table_names

ALL_TABLES = (
    "schema_migrations",
    "state_clock",
    "jobs",
    "stage_runs",
    "leases",
    "approval_refs",
    "cache_pointers",
)


def test_migrate_empty_db_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"

    with StateStore.open(db_path) as store:
        assert store.applied_schema_version() == LATEST_VERSION
        pragmas = store.pragma_state()

    assert pragmas.journal_mode == "wal"
    assert pragmas.foreign_keys is True
    assert pragmas.synchronous == 2
    tables = table_names(db_path)
    for required in ALL_TABLES:
        assert required in tables


def test_reopen_applies_no_pending_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"

    StateStore.open(db_path).close()
    with StateStore.open(db_path) as store:
        assert store.applied_schema_version() == LATEST_VERSION

    connection = open_raw(db_path)
    try:
        applied = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()
    finally:
        connection.close()
    assert int(applied[0]) == len(MIGRATIONS)


def test_upgrade_from_previous_version_preserves_data(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"

    connection = open_raw(db_path)
    apply_migrations(connection, MIGRATIONS[:1])
    connection.execute(
        "INSERT INTO jobs (job_id, episode_id, current_stage, status,"
        " created_at_seq, updated_at_seq)"
        " VALUES ('job-x', 'ep-1', 'ingest', 'CREATED', 1, 1)"
    )
    connection.close()
    assert "approval_refs" not in table_names(db_path)

    with StateStore.open(db_path) as store:
        assert store.applied_schema_version() == LATEST_VERSION
        snapshot = store.get_job_snapshot("job-x")

    assert snapshot.job.episode_id == "ep-1"
    assert snapshot.job.status == "CREATED"
    assert "approval_refs" in table_names(db_path)


def test_interrupted_migration_rolls_back_to_previous_version(tmp_path: Path) -> None:
    base_path = tmp_path / "v1.sqlite3"
    connection = open_raw(base_path)
    apply_migrations(connection, MIGRATIONS[:1])
    connection.close()

    fault = Migration(
        version=2,
        name="fault-injected",
        statements=(
            "CREATE TABLE mid_migration_table (id INTEGER PRIMARY KEY)",
            "CREATE TABLE mid_migration_table (id INTEGER PRIMARY KEY)",
        ),
    )
    fault_migrations = (*MIGRATIONS[:1], fault)

    for attempt in (1, 2):
        copy_path = tmp_path / f"fault-{attempt}.sqlite3"
        shutil.copyfile(base_path, copy_path)

        fault_connection = open_raw(copy_path)
        with pytest.raises(MigrationError, match="migration-failed"):
            apply_migrations(fault_connection, fault_migrations)
        fault_connection.close()

        assert read_schema_version(copy_path) == 1
        assert "mid_migration_table" not in table_names(copy_path)

        with StateStore.open(copy_path) as store:
            assert store.applied_schema_version() == LATEST_VERSION


def test_unknown_schema_version_refuses_to_open(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    StateStore.open(db_path).close()

    connection = open_raw(db_path)
    connection.execute(
        "INSERT INTO schema_migrations (version, name, sha256)"
        " VALUES (999, 'future-version', '00000000000000000000000000000000"
        "00000000000000000000000000000000')"
    )
    connection.close()

    with pytest.raises(MigrationError) as error:
        StateStore.open(db_path)
    assert error.value.code == "unknown-schema-version"
    assert "999" in str(error.value)


def test_tampered_schema_migrations_refuses_to_open(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    StateStore.open(db_path).close()

    connection = open_raw(db_path)
    connection.execute(
        "UPDATE schema_migrations SET sha256 = '00000000000000000000000000000000"
        "00000000000000000000000000000000' WHERE version = 1"
    )
    connection.close()

    with pytest.raises(MigrationError) as error:
        StateStore.open(db_path)
    assert error.value.code == "migration-hash-mismatch"
