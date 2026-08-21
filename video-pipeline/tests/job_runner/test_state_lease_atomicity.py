"""Blocker-fix regression: SQL lease acquisition/renew/release atomicity.

The old SELECT-then-upsert sequence could double-acquire: a reader that
passed the freshness SELECT before a concurrent writer committed would
blind-upsert over the fresh lease. These tests interleave a real
uncommitted writer exactly in that window and require the typed refusal.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_store import StateStore

DB_NAME = "state.sqlite3"
RESOURCE = "resolve-builder"
COMMIT_DELAY_SECONDS = 0.3


def _open_store(path: Path) -> StateStore:
    return StateStore.open(path / DB_NAME)


def _uncommitted_steal(path: Path, *, holder: str, expires_at: int) -> sqlite3.Connection:
    """Hold an open write transaction that claims the lease, uncommitted."""

    writer = sqlite3.connect(path / DB_NAME, isolation_level=None, timeout=10)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO leases (resource, holder, expires_at) VALUES (?, ?, ?)"
        " ON CONFLICT(resource) DO UPDATE SET holder = excluded.holder,"
        " expires_at = excluded.expires_at",
        (RESOURCE, holder, expires_at),
    )
    return writer


def test_acquire_cannot_double_acquire_across_uncommitted_writer(tmp_path: Path) -> None:
    with _open_store(tmp_path):
        thief = _uncommitted_steal(tmp_path, holder="writer-b", expires_at=10_000)
        outcome: dict[str, object] = {}

        def acquire() -> None:
            with _open_store(tmp_path) as racer_store:
                try:
                    racer_store.acquire_lease(
                        resource=RESOURCE, holder="writer-a", now=1_000, ttl_seconds=60
                    )
                    outcome["result"] = "acquired"
                except StateStoreError as error:
                    outcome["result"] = error.code

        racer = threading.Thread(target=acquire)
        racer.start()
        time.sleep(COMMIT_DELAY_SECONDS)
        thief.commit()
        thief.close()
        racer.join(timeout=15)

        assert outcome["result"] == "lease-held"
        with _open_store(tmp_path) as verify:
            row = verify._connection.execute(
                "SELECT holder FROM leases WHERE resource = ?", (RESOURCE,)
            ).fetchone()
            assert row is not None
            assert str(row[0]) == "writer-b"


def test_renew_cannot_overwrite_a_stolen_lease(tmp_path: Path) -> None:
    with _open_store(tmp_path) as store:
        store.acquire_lease(
            resource=RESOURCE, holder="writer-a", now=1_000, ttl_seconds=60
        )
        thief = _uncommitted_steal(tmp_path, holder="writer-b", expires_at=2_000)
        outcome: dict[str, object] = {}

        def renew() -> None:
            with _open_store(tmp_path) as racer_store:
                try:
                    racer_store.renew_lease(
                        resource=RESOURCE, holder="writer-a", now=1_030, ttl_seconds=60
                    )
                    outcome["result"] = "renewed"
                except StateStoreError as error:
                    outcome["result"] = error.code

        racer = threading.Thread(target=renew)
        racer.start()
        time.sleep(COMMIT_DELAY_SECONDS)
        thief.commit()
        thief.close()
        racer.join(timeout=15)

        assert outcome["result"] in {"not-holder", "lease-held"}
        with _open_store(tmp_path) as verify:
            row = verify._connection.execute(
                "SELECT holder, expires_at FROM leases WHERE resource = ?", (RESOURCE,)
            ).fetchone()
            assert row is not None
            assert int(row[1]) == 2_000


def test_release_cannot_delete_a_stolen_lease(tmp_path: Path) -> None:
    with _open_store(tmp_path) as store:
        store.acquire_lease(
            resource=RESOURCE, holder="writer-a", now=1_000, ttl_seconds=60
        )
        thief = _uncommitted_steal(tmp_path, holder="writer-b", expires_at=2_000)
        outcome: dict[str, object] = {}

        def release() -> None:
            with _open_store(tmp_path) as racer_store:
                try:
                    racer_store.release_lease(resource=RESOURCE, holder="writer-a", now=1_030)
                    outcome["result"] = "released"
                except StateStoreError as error:
                    outcome["result"] = error.code

        racer = threading.Thread(target=release)
        racer.start()
        time.sleep(COMMIT_DELAY_SECONDS)
        thief.commit()
        thief.close()
        racer.join(timeout=15)

        assert outcome["result"] in {"not-holder", "lease-held"}
        with _open_store(tmp_path) as verify:
            row = verify._connection.execute(
                "SELECT holder FROM leases WHERE resource = ?", (RESOURCE,)
            ).fetchone()
            assert row is not None
            assert str(row[0]) == "writer-b"


def test_second_process_same_role_refused_while_lease_fresh(tmp_path: Path) -> None:
    """Holder tokens are invocation-unique: only expiry (or the owner's
    renew_lease) can extend a fresh lease — a second process claiming the
    same ROLE with its own token is refused."""

    with _open_store(tmp_path) as store:
        owner_holder = "stage-runner:11111111-1111-4111-8111-111111111111"
        rival_holder = "stage-runner:22222222-2222-4222-8222-222222222222"
        store.acquire_lease(
            resource=RESOURCE, holder=owner_holder, now=1_000, ttl_seconds=60
        )
        with pytest.raises(StateStoreError, match="lease-held"):
            store.acquire_lease(
                resource=RESOURCE, holder=rival_holder, now=1_010, ttl_seconds=60
            )
        renewed = store.renew_lease(
            resource=RESOURCE, holder=owner_holder, now=1_020, ttl_seconds=60
        )
        assert renewed.holder == owner_holder
        with pytest.raises(StateStoreError) as rival_renew:
            store.renew_lease(
                resource=RESOURCE, holder=rival_holder, now=1_030, ttl_seconds=60
            )
        assert rival_renew.value.code == "not-holder"
        expired_takeover = store.acquire_lease(
            resource=RESOURCE, holder=rival_holder, now=1_100, ttl_seconds=60
        )
        assert expired_takeover.holder == rival_holder


def test_same_token_reacquire_while_fresh_is_refused(tmp_path: Path) -> None:
    """Even the SAME token cannot re-acquire a fresh lease; renewal is the
    owner's only refresh path (renew_lease), keeping acquisition honest."""

    with _open_store(tmp_path) as store:
        holder = "resolve-builder:33333333-3333-4333-8333-333333333333"
        store.acquire_lease(resource=RESOURCE, holder=holder, now=1_000, ttl_seconds=60)
        with pytest.raises(StateStoreError, match="lease-held"):
            store.acquire_lease(
                resource=RESOURCE, holder=holder, now=1_005, ttl_seconds=60
            )
        store.renew_lease(resource=RESOURCE, holder=holder, now=1_010, ttl_seconds=60)


def test_parallel_distinct_token_acquires_never_share_a_timestamp(
    tmp_path: Path,
) -> None:
    with _open_store(tmp_path):
        winners: dict[int, str] = {}
        errors: list[StateStoreError] = []
        lock = threading.Lock()

        def acquire(worker: str) -> None:
            with _open_store(tmp_path) as racer_store:
                for now in range(0, 200, 2):
                    try:
                        lease = racer_store.acquire_lease(
                            resource=RESOURCE,
                            holder=f"stage-runner:{worker}",
                            now=now,
                            ttl_seconds=1,
                        )
                    except StateStoreError as error:
                        if error.code != "lease-held":
                            errors.append(error)
                        continue
                    with lock:
                        previous = winners.setdefault(now, lease.holder)
                        assert previous == lease.holder

        threads = [
            threading.Thread(target=acquire, args=(f"worker-{index}",))
            for index in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert errors == []
        assert winners
