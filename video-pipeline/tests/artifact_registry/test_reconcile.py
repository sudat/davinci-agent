from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from services.artifact_registry.models import RegistryIndex, mint_index
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


def test_registry_save_is_private_single_writer_primitive() -> None:
    """The only mutation surface is the lock-held apply(); a public save
    would let any caller bypass the critical section (round-2 blocker)."""

    from services.artifact_registry.registry import ArtifactRegistry  # noqa: PLC0415

    assert not hasattr(ArtifactRegistry, "save")
    assert hasattr(ArtifactRegistry, "apply")


def test_apply_transforms_never_interleave(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    inside = 0
    peak: dict[str, int] = {}
    guard = threading.Lock()

    def transform(index: RegistryIndex) -> RegistryIndex:
        nonlocal inside
        with guard:
            inside += 1
            peak["peak"] = max(peak.get("peak", 0), inside)
        time.sleep(0.05)
        with guard:
            inside -= 1
        return index

    threads = [
        threading.Thread(target=lambda: registry.apply(transform)) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert peak["peak"] == 1


def test_concurrent_reconcile_and_register_lose_nothing(tmp_path: Path) -> None:
    """All index mutations serialize through one lock-held
    reload-transform-save primitive: adoption (reconcile) and fresh
    registrations interleaved from parallel threads must ALL survive."""

    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    orphan_receipts = [
        publish(store, f"orphan-{i}", f"orphan-payload-{i}".encode())
        for i in range(6)
    ]
    fresh_receipts = [
        publish(store, f"fresh-{i}", f"fresh-payload-{i}".encode()) for i in range(6)
    ]

    errors: list[Exception] = []

    def reconciler() -> None:
        try:
            for _ in range(4):
                reconcile(store, registry)
        except Exception as error:  # noqa: BLE001 (test collects every failure)
            errors.append(error)

    def registrar(receipts: list[object]) -> None:
        try:
            for receipt in receipts:
                registry.register(store, receipt)  # type: ignore[arg-type]
        except Exception as error:  # noqa: BLE001 (test collects every failure)
            errors.append(error)

    threads = [
        threading.Thread(target=reconciler),
        threading.Thread(target=registrar, args=(orphan_receipts,)),
        threading.Thread(target=registrar, args=(fresh_receipts,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    final = registry.load()
    assert set(final.entries) == {
        *(f"orphan-{i}" for i in range(6)),
        *(f"fresh-{i}" for i in range(6)),
    }
    assert len(final.entries) == 12


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
    tampered_bytes = canonical_model_bytes(
        mint_index(index.entries | {"art-a": tampered_size})
    )
    registry.index_path.write_bytes(tampered_bytes)
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
