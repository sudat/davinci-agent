from __future__ import annotations

from pathlib import Path

from services.artifact_store.reconcile import reconcile
from services.artifact_store.store import ArtifactStore
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


def test_crash_after_rename_before_meta_recovers_idempotently(tmp_path: Path) -> None:
    manifest = load_manifest("cp-crash-before-rename")
    store_root = tmp_path / "store"
    store = ArtifactStore(store_root)
    intent = intent_for(manifest)
    payload = payload_for(manifest)
    content_sha256 = intent.envelope.content_hash

    store.journal_intent(intent)
    store.write_object_temp(content_sha256, payload)
    store.rename_object(content_sha256)
    # fault: killed after rename, before the meta sidecar

    assert object_path(store_root, content_sha256).exists()
    assert not meta_path(store_root, manifest.scenario.artifact_id).exists()

    report = reconcile(store_root)

    assert report.entries == ()

    receipt = store.publish(intent, payload)

    assert receipt.idempotent is True
    assert meta_path(store_root, manifest.scenario.artifact_id).exists()
    reopened, _ref = store.reopen(content_sha256)
    assert reopened == payload


def test_stale_temp_with_final_present_is_discarded(tmp_path: Path) -> None:
    manifest = load_manifest("cp-atomic-publish")
    store_root = tmp_path / "store"
    store = ArtifactStore(store_root)
    intent = intent_for(manifest)
    payload = payload_for(manifest)
    store.publish(intent, payload)

    # plant a leftover temp even though the final object exists
    temp_path(store_root, intent.envelope.content_hash).write_bytes(payload)

    report = reconcile(store_root)

    assert len(report.entries) == 1
    assert report.entries[0].action == "discard-stale-temp"
    assert not temp_path(store_root, intent.envelope.content_hash).exists()
    reopened, _ref = store.reopen(intent.envelope.content_hash)
    assert reopened == payload


def test_reconcile_is_deterministic_and_journaled(tmp_path: Path) -> None:
    manifest = load_manifest("cp-orphan-reconcile")
    store_root = tmp_path / "store"
    ArtifactStore(store_root)
    payload = payload_for(manifest)
    content_sha256 = manifest.scenario.payload.sha256
    shard_dir = store_root / "objects" / content_sha256[:2]
    shard_dir.mkdir(parents=True, exist_ok=True)
    temp_path(store_root, content_sha256).write_bytes(payload)

    first = reconcile(store_root)
    second = reconcile(store_root)

    assert first.entries != ()
    assert second.entries == ()
    assert not temp_path(store_root, content_sha256).exists()
    assert not journal_path(store_root, content_sha256).exists()
    lines = reconcile_log_lines(store_root)
    assert len(lines) == 2
    assert lines[0].entries[0].action == "discard-orphan-temp"
    assert lines[1].entries == ()
