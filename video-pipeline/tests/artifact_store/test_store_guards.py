from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.artifact_store.models import PublicationIntent
from services.artifact_store.store import ArtifactStore, StoreRefusalError, resolve_within
from tests.artifact_store.support import (
    intent_for,
    load_manifest,
    object_path,
    payload_for,
    temp_path,
)


def test_path_escape_components_are_refused(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")

    with pytest.raises(StoreRefusalError, match="path-escape"):
        resolve_within(store.store_root, ("..", "outside"))
    with pytest.raises(StoreRefusalError, match="path-escape"):
        resolve_within(store.store_root, ("objects", ".."))
    with pytest.raises(StoreRefusalError, match="path-escape"):
        resolve_within(store.store_root, ("absolute/path",))
    with pytest.raises(StoreRefusalError, match="path-escape"):
        resolve_within(store.store_root, ("",))


def test_resolve_within_accepts_plain_store_layout(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")

    resolved = resolve_within(store.store_root, ("objects", "ab"))

    assert resolved == store.store_root / "objects" / "ab"


def test_symlinked_objects_dir_denies_publication(tmp_path: Path) -> None:
    manifest = load_manifest("cp-atomic-publish")
    store_root = tmp_path / "store"
    ArtifactStore(store_root)
    (tmp_path / "real-objects").mkdir()
    (store_root / "objects").symlink_to(tmp_path / "real-objects")

    store = ArtifactStore(store_root)
    intent = intent_for(manifest)
    payload = payload_for(manifest)

    with pytest.raises(StoreRefusalError, match="symlinked-path-component"):
        store.publish(intent, payload)


def test_symlinked_artifacts_dir_denies_publication(tmp_path: Path) -> None:
    manifest = load_manifest("cp-atomic-publish")
    store_root = tmp_path / "store"
    ArtifactStore(store_root)
    (tmp_path / "real-artifacts").mkdir()
    (store_root / "artifacts").symlink_to(tmp_path / "real-artifacts")

    store = ArtifactStore(store_root)
    intent = intent_for(manifest)
    payload = payload_for(manifest)

    with pytest.raises(StoreRefusalError, match="symlinked-path-component"):
        store.publish(intent, payload)
    assert not object_path(store_root, intent.envelope.content_hash).exists()


def test_payload_hash_mismatch_is_refused(tmp_path: Path) -> None:
    manifest = load_manifest("cp-atomic-publish")
    store = ArtifactStore(tmp_path / "store")
    intent = intent_for(manifest)

    with pytest.raises(StoreRefusalError, match="content-hash-mismatch"):
        store.publish(intent, b"payload bytes do not match the intent hash")


def test_same_bytes_republish_is_idempotent(tmp_path: Path) -> None:
    manifest = load_manifest("cp-atomic-publish")
    store = ArtifactStore(tmp_path / "store")
    intent = intent_for(manifest)
    payload = payload_for(manifest)

    first = store.publish(intent, payload)
    second = store.publish(intent, payload)

    assert first.idempotent is False
    assert second.idempotent is True
    assert second.content_sha256 == first.content_sha256


def test_republish_after_tamper_is_refused(tmp_path: Path) -> None:
    manifest = load_manifest("cp-atomic-publish")
    store = ArtifactStore(tmp_path / "store")
    intent = intent_for(manifest)
    payload = payload_for(manifest)
    store.publish(intent, payload)

    object_path(tmp_path / "store", intent.envelope.content_hash).write_bytes(b"drifted")

    with pytest.raises(StoreRefusalError, match="overwrite-refused"):
        store.publish(intent, payload)


def test_artifact_id_reuse_with_different_content_is_refused(tmp_path: Path) -> None:
    manifest = load_manifest("cp-atomic-publish")
    store = ArtifactStore(tmp_path / "store")
    intent = intent_for(manifest)
    payload = payload_for(manifest)
    store.publish(intent, payload)

    other_payload = b"different bytes for the same artifact id"
    other_intent = intent.model_copy(
        update={
            "envelope": intent.envelope.model_copy(
                update={"content_hash": hashlib.sha256(other_payload).hexdigest()}
            )
        }
    )

    with pytest.raises(StoreRefusalError, match="meta-overwrite-refused"):
        store.publish(other_intent, other_payload)


def test_reopen_missing_object_is_refused(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    missing = "ab" * 32

    with pytest.raises(StoreRefusalError, match="object-missing"):
        store.reopen(missing)


def test_malformed_intent_json_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PublicationIntent.model_validate_json(b"{not json")


def test_malformed_journal_drift_is_refused(tmp_path: Path) -> None:
    manifest = load_manifest("cp-atomic-publish")
    store_root = tmp_path / "store"
    store = ArtifactStore(store_root)
    intent = intent_for(manifest)
    payload = payload_for(manifest)
    content_sha256 = intent.envelope.content_hash

    shard_dir = store_root / "objects" / content_sha256[:2]
    shard_dir.mkdir(parents=True)
    temp_path(store_root, content_sha256).write_bytes(payload)
    (store_root / "journal").mkdir()
    (store_root / "journal" / f"{content_sha256}.json").write_bytes(b"{malformed")

    with pytest.raises(StoreRefusalError, match="journal-drift"):
        store.publish(intent, payload)


def test_symlinked_object_file_reopen_is_refused(tmp_path: Path) -> None:
    manifest = load_manifest("cp-atomic-publish")
    store_root = tmp_path / "store"
    ArtifactStore(store_root)
    intent = intent_for(manifest)
    payload = payload_for(manifest)
    content_sha256 = intent.envelope.content_hash

    outside = tmp_path / "outside.bin"
    outside.write_bytes(payload)
    object_file = object_path(store_root, content_sha256)
    object_file.parent.mkdir(parents=True, exist_ok=True)
    object_file.symlink_to(outside)

    store = ArtifactStore(store_root)
    with pytest.raises(StoreRefusalError, match="symlinked-path-component"):
        store.reopen(content_sha256)
