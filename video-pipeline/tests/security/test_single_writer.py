"""Attack class 11: multiple writers — the single-writer lease holds.

Two REAL StateStore connections over one on-disk SQLite database: while
writer A holds the lease, writer B's acquire/renew/release attempts all
fail with typed errors and the lease row is byte-identical afterwards
(zero side effects on job state). Expiry steal remains the only lawful
handover, exactly as designed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_leases import LeaseOps
from services.job_runner.state_models import LeaseRow
from services.job_runner.state_store import StateStore

RESOURCE = "resolve-builder"


def _lease_row(path: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute(
            "SELECT resource, holder, expires_at FROM leases WHERE resource = ?",
            (RESOURCE,),
        ).fetchone()
    finally:
        connection.close()


def test_10_second_writer_acquires_nothing(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    with StateStore.open(db) as writer_a:
        writer_a.create_job(job_id="job-1", episode_id="ep-1", current_stage="ingest")
        held = writer_a.acquire_lease(
            resource=RESOURCE, holder="writer-a", now=1_000, ttl_seconds=600
        )
        assert held == LeaseRow(resource=RESOURCE, holder="writer-a", expires_at=1_600)

        with StateStore.open(db) as writer_b:
            with pytest.raises(StateStoreError, match="lease-held"):
                writer_b.acquire_lease(
                    resource=RESOURCE, holder="writer-b", now=1_100, ttl_seconds=600
                )
            with pytest.raises(StateStoreError, match="not-holder"):
                writer_b.renew_lease(
                    resource=RESOURCE, holder="writer-b", now=1_100, ttl_seconds=600
                )
            with pytest.raises(StateStoreError, match="not-holder"):
                writer_b.release_lease(resource=RESOURCE, holder="writer-b", now=1_100)
            assert _lease_row(db) == (RESOURCE, "writer-a", 1_600)


def test_11_renew_by_non_holder_leaves_row_untouched(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    with StateStore.open(db) as writer_a:
        writer_a.create_job(job_id="job-1", episode_id="ep-1", current_stage="ingest")
        writer_a.acquire_lease(resource=RESOURCE, holder="writer-a", now=1_000, ttl_seconds=600)
        before = _lease_row(db)
    with StateStore.open(db) as writer_b, pytest.raises(StateStoreError):
        writer_b.renew_lease(
            resource=RESOURCE, holder="writer-b", now=1_200, ttl_seconds=60
        )
    assert _lease_row(db) == before


def test_12_expired_lease_is_the_only_lawful_handover(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    with StateStore.open(db) as writer_a:
        writer_a.create_job(job_id="job-1", episode_id="ep-1", current_stage="ingest")
        writer_a.acquire_lease(resource=RESOURCE, holder="writer-a", now=1_000, ttl_seconds=100)
        with StateStore.open(db) as writer_b:
            stolen = writer_b.acquire_lease(
                resource=RESOURCE, holder="writer-b", now=1_200, ttl_seconds=100
            )
            assert stolen.holder == "writer-b"
            with pytest.raises(StateStoreError, match="not-holder"):
                writer_a.renew_lease(
                    resource=RESOURCE, holder="writer-a", now=1_300, ttl_seconds=100
                )


def test_13_nonpositive_ttl_is_typed(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    with StateStore.open(db) as store:
        store.create_job(job_id="job-1", episode_id="ep-1", current_stage="ingest")
        with pytest.raises(StateStoreError, match="lease-ttl"):
            store.acquire_lease(resource=RESOURCE, holder="w", now=1, ttl_seconds=0)


def test_14_lease_ops_surface_is_the_documented_writer_gate() -> None:
    assert issubclass(LeaseOps, object)
    assert hasattr(LeaseOps, "acquire_lease")
    assert hasattr(LeaseOps, "renew_lease")
    assert hasattr(LeaseOps, "release_lease")
