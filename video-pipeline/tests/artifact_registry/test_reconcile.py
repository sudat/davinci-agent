from __future__ import annotations

from pathlib import Path

import pytest

from services.artifact_registry.models import mint_index
from services.artifact_registry.reconcile import (
    RegistryDriftError,
    rebuild_index_from_store,
    reconcile,
)
from services.artifact_registry.registry import MissingFinding
from services.contracts.primitives import Producer
from services.foundation_io import canonical_model_bytes, sha256_file
from tests.artifact_registry.support import (
    journal_events,
    make_registry,
    make_store,
    object_file,
    publish,
    sha256_bytes,
)
from tests.artifact_store.support import intent_for, load_manifest, payload_for

WRONG_SHA256 = "f" * 64


def test_reconcile_complete_and_orphaned_publish(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    complete = load_manifest("cp-atomic-publish")
    orphan = load_manifest("cp-orphan-reconcile")
    complete_receipt = store.publish(intent_for(complete), payload_for(complete))
    registry.register(store, complete_receipt)
    store.publish(intent_for(orphan), payload_for(orphan))

    report = reconcile(store, registry)

    assert [entry.artifact_id for entry in report.adopted] == [
        orphan.scenario.artifact_id
    ]
    assert report.missing == ()
    index = registry.load()
    assert set(index.entries) == {
        complete.scenario.artifact_id,
        orphan.scenario.artifact_id,
    }
    adopted_entry = index.entries[orphan.scenario.artifact_id]
    assert adopted_entry.content_sha256 == orphan.scenario.payload.sha256
    events = journal_events(tmp_path / "registry")
    assert events[-1].event == "reconcile"
    assert events[-1].adopted_ids == (orphan.scenario.artifact_id,)
    second = reconcile(store, registry)
    assert second.adopted == ()


def test_missing_file_detection_on_tmp_copy(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    receipt_a = publish(store, "art-a", b"payload-a")
    receipt_b = publish(store, "art-b", b"payload-b")
    registry.register(store, receipt_a)
    registry.register(store, receipt_b)

    assert registry.detect_missing(store) == ()

    object_file(tmp_path, receipt_b.content_sha256).unlink()
    assert registry.detect_missing(store) == (
        MissingFinding(
            artifact_id="art-b",
            content_sha256=receipt_b.content_sha256,
            reason="object-absent",
        ),
    )

    index = registry.load()
    tampered_size = index.entries["art-a"].model_copy(update={"size": 999})
    registry.save(mint_index(index.entries | {"art-a": tampered_size}))
    assert registry.detect_missing(store) == (
        MissingFinding(
            artifact_id="art-a",
            content_sha256=receipt_a.content_sha256,
            reason="size-mismatch",
        ),
        MissingFinding(
            artifact_id="art-b",
            content_sha256=receipt_b.content_sha256,
            reason="object-absent",
        ),
    )


def test_reconcile_reports_missing_without_dropping(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    receipt_a = publish(store, "art-a", b"payload-a")
    receipt_b = publish(store, "art-b", b"payload-b")
    registry.register(store, receipt_a)
    registry.register(store, receipt_b)
    object_file(tmp_path, receipt_b.content_sha256).unlink()

    report = reconcile(store, registry)

    assert report.missing == (
        MissingFinding(
            artifact_id="art-b",
            content_sha256=receipt_b.content_sha256,
            reason="object-absent",
        ),
    )
    assert "art-b" in registry.load().entries


def test_registry_wrong_hash_drift_rebuild_restores_truth(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    receipt = publish(store, "art-a", b"payload-a")
    registry.register(store, receipt)
    stored_object = object_file(tmp_path, receipt.content_sha256)
    object_hash_before = sha256_file(stored_object)
    assert object_hash_before == receipt.content_sha256

    index = registry.load()
    tampered = index.entries["art-a"].model_copy(
        update={"content_sha256": WRONG_SHA256}
    )
    resealed = mint_index(index.entries | {"art-a": tampered})
    registry.index_path.write_bytes(canonical_model_bytes(resealed))
    assert registry.load().entries["art-a"].content_sha256 == WRONG_SHA256

    with pytest.raises(RegistryDriftError) as error:
        reconcile(store, registry)
    assert error.value.findings[0].artifact_id == "art-a"
    assert error.value.findings[0].reason == "meta-hash-mismatch"

    rebuilt = rebuild_index_from_store(store, registry)
    assert rebuilt.entries["art-a"].content_sha256 == receipt.content_sha256
    assert registry.load().entries["art-a"].content_sha256 == receipt.content_sha256
    assert sha256_file(stored_object) == object_hash_before

    report = reconcile(store, registry)
    assert report.adopted == ()
    assert report.missing == ()
    assert journal_events(tmp_path / "registry")[-1].event == "index-rebuilt"


def test_bare_object_without_meta_is_not_adopted(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    payload = b"lonely-object"
    digest = sha256_bytes(payload)
    shard = tmp_path / "store" / "objects" / digest[:2]
    shard.mkdir(parents=True)
    (shard / digest).write_bytes(payload)

    report = reconcile(store, registry)

    assert report.adopted == ()
    assert report.unadopted_object_sha256s == (digest,)
    assert not registry.load().entries


def test_rebuilt_entries_mirror_store_meta(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    producer = Producer(name="rebuild-producer", version="v2")
    receipt = publish(store, "art-a", b"payload-a", producer=producer)
    registry.register(store, receipt)

    rebuilt = rebuild_index_from_store(store, registry)

    entry = rebuilt.entries["art-a"]
    assert entry.producer == producer
    assert entry.size == len(b"payload-a")
    assert entry.artifact_type == "registry-test-artifact"
    assert entry.schema_version == "registry-test-v1"
