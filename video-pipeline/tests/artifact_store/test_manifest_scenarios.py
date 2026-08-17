from __future__ import annotations

from pathlib import Path

import pytest

from services.artifact_store.lease import LeaseAuthority, LeaseError
from services.artifact_store.reconcile import reconcile
from services.artifact_store.store import ArtifactStore, StoreRefusalError
from tests.artifact_store.support import (
    intent_for,
    journal_path,
    load_manifest,
    meta_path,
    object_path,
    payload_for,
    reconcile_log_lines,
    temp_path,
)


def test_cp_atomic_publish(tmp_path: Path) -> None:
    manifest = load_manifest("cp-atomic-publish")
    store = ArtifactStore(tmp_path / "store")
    intent = intent_for(manifest)
    payload = payload_for(manifest)
    content_sha256 = intent.envelope.content_hash

    receipt = store.publish(intent, payload)

    assert receipt.idempotent is False
    assert receipt.content_sha256 == content_sha256
    assert object_path(tmp_path / "store", content_sha256).read_bytes() == payload
    assert meta_path(tmp_path / "store", manifest.scenario.artifact_id).exists()
    assert not temp_path(tmp_path / "store", content_sha256).exists()
    reopened, ref = store.reopen(content_sha256)
    assert reopened == payload
    assert ref.sha256 == content_sha256
    assert ref.size == len(payload)


def test_cp_crash_before_rename(tmp_path: Path) -> None:
    manifest = load_manifest("cp-crash-before-rename")
    store = ArtifactStore(tmp_path / "store")
    intent = intent_for(manifest)
    payload = payload_for(manifest)
    content_sha256 = intent.envelope.content_hash

    store.journal_intent(intent)
    store.write_object_temp(content_sha256, payload)

    assert not object_path(tmp_path / "store", content_sha256).exists()
    assert temp_path(tmp_path / "store", content_sha256).exists()
    assert journal_path(tmp_path / "store", content_sha256).exists()

    report = reconcile(tmp_path / "store")

    assert len(report.entries) == 1
    assert report.entries[0].action == "discard-crashed-temp"
    assert not temp_path(tmp_path / "store", content_sha256).exists()
    assert not object_path(tmp_path / "store", content_sha256).exists()

    receipt = store.publish(intent, payload)

    assert receipt.idempotent is False
    reopened, _ref = store.reopen(content_sha256)
    assert reopened == payload


def test_cp_crash_before_rename_resumes_without_reconcile(tmp_path: Path) -> None:
    manifest = load_manifest("cp-crash-before-rename")
    store = ArtifactStore(tmp_path / "store")
    intent = intent_for(manifest)
    payload = payload_for(manifest)

    store.journal_intent(intent)
    store.write_object_temp(intent.envelope.content_hash, payload)

    receipt = store.publish(intent, payload)

    assert receipt.idempotent is False
    assert object_path(tmp_path / "store", intent.envelope.content_hash).read_bytes() == payload
    assert meta_path(tmp_path / "store", manifest.scenario.artifact_id).exists()


def test_cp_orphan_reconcile(tmp_path: Path) -> None:
    manifest = load_manifest("cp-orphan-reconcile")
    store_root = tmp_path / "store"
    ArtifactStore(store_root)
    payload = payload_for(manifest)
    content_sha256 = manifest.scenario.payload.sha256

    shard_dir = store_root / "objects" / content_sha256[:2]
    shard_dir.mkdir(parents=True, exist_ok=True)
    temp_path(store_root, content_sha256).write_bytes(payload)

    assert temp_path(store_root, content_sha256).exists()
    assert not journal_path(store_root, content_sha256).exists()

    report = reconcile(store_root)

    assert len(report.entries) == 1
    assert report.entries[0].action == "discard-orphan-temp"
    assert not temp_path(store_root, content_sha256).exists()
    assert not object_path(store_root, content_sha256).exists()
    assert len(reconcile_log_lines(store_root)) == 1


def test_cp_stale_cas(tmp_path: Path) -> None:
    manifest = load_manifest("cp-stale-cas")
    store = ArtifactStore(tmp_path / "store")
    intent = intent_for(manifest)
    payload = payload_for(manifest)
    content_sha256 = intent.envelope.content_hash

    store.publish(intent, payload)
    object_path(tmp_path / "store", content_sha256).write_bytes(b"tampered-bytes")

    with pytest.raises(StoreRefusalError, match="content-hash-mismatch") as error:
        store.reopen(content_sha256)
    assert error.value.code == "content-hash-mismatch"


def test_cp_lease_expiry(tmp_path: Path) -> None:
    manifest = load_manifest("cp-lease-expiry")
    lease_spec = manifest.scenario.lease
    assert lease_spec is not None
    authority = LeaseAuthority(tmp_path / "store" / "leases")
    now = 1_000_000

    record = authority.acquire(
        "cp-baseline",
        lease_spec.holder,
        now_unix=now,
        ttl_seconds=lease_spec.ttl_seconds,
    )
    assert record.holder == lease_spec.holder

    expired_now = now + lease_spec.ttl_seconds + lease_spec.elapsed_past_expiry_seconds
    with pytest.raises(LeaseError, match="lease-expired") as holder_error:
        authority.commit("cp-baseline", lease_spec.holder, now_unix=expired_now)
    assert holder_error.value.code == "lease-expired"

    granted = authority.acquire(
        "cp-baseline",
        lease_spec.challenger,
        now_unix=expired_now,
        ttl_seconds=lease_spec.ttl_seconds,
    )
    assert granted.holder == lease_spec.challenger
    accepted = authority.commit(
        "cp-baseline", lease_spec.challenger, now_unix=expired_now
    )
    assert accepted.holder == lease_spec.challenger


def test_cp_path_symlink_denial(tmp_path: Path) -> None:
    manifest = load_manifest("cp-path-symlink-denial")
    store_root = tmp_path / "store"
    store = ArtifactStore(store_root)
    intent = intent_for(manifest)
    payload = payload_for(manifest)
    content_sha256 = intent.envelope.content_hash

    outside = tmp_path / "outside"
    outside.mkdir()
    shard_dir = store_root / "objects" / content_sha256[:2]
    shard_dir.parent.mkdir(parents=True, exist_ok=True)
    shard_dir.symlink_to(outside)

    with pytest.raises(StoreRefusalError, match="symlinked-path-component") as error:
        store.publish(intent, payload)
    assert error.value.code == "symlinked-path-component"
    assert not object_path(store_root, content_sha256).exists()
    assert not (outside / content_sha256).exists()
