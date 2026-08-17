from __future__ import annotations

from pathlib import Path

import pytest

from services.artifact_registry.registry import RegistryError
from services.contracts.primitives import ArtifactRef
from tests.artifact_registry.support import (
    GHOST_SHA256,
    make_registry,
    make_store,
    object_file,
    publish,
    publish_duplicate_id,
    sha256_bytes,
)


def test_register_and_walk_three_level_dag(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    receipt_a = publish(store, "art-a", b"payload-a")
    receipt_b = publish(
        store,
        "art-b",
        b"payload-b",
        inputs=(ArtifactRef(artifact_id="art-a", sha256=receipt_a.content_sha256),),
    )
    receipt_c = publish(
        store,
        "art-c",
        b"payload-c",
        inputs=(ArtifactRef(artifact_id="art-b", sha256=receipt_b.content_sha256),),
    )
    for receipt in (receipt_a, receipt_b, receipt_c):
        registry.register(store, receipt)

    nodes = registry.walk(store, "art-c", depth=3)

    assert [node.entry.artifact_id for node in nodes] == ["art-c", "art-b", "art-a"]
    assert [node.depth for node in nodes] == [0, 1, 2]
    limited = registry.walk(store, "art-c", depth=1)
    assert [node.entry.artifact_id for node in limited] == ["art-c", "art-b"]


def test_register_is_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    receipt = publish(store, "art-a", b"payload-a")

    first = registry.register(store, receipt)
    before = registry.index_path.read_bytes()
    second = registry.register(store, receipt)

    assert second == first
    assert registry.index_path.read_bytes() == before


def test_duplicate_hash_reuse_keeps_both_ids(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    payload = b"shared-payload"

    receipt_one = publish(store, "art-one", payload)
    receipt_two = publish_duplicate_id(store, "art-two", payload)
    assert receipt_two.idempotent is True

    registry.register(store, receipt_one)
    registry.register(store, receipt_two)

    index = registry.load()
    digest = sha256_bytes(payload)
    assert index.entries["art-one"].content_sha256 == digest
    assert index.entries["art-two"].content_sha256 == digest
    assert set(index.by_sha256[digest]) == {"art-one", "art-two"}
    reopened, object_ref = store.reopen(digest)
    assert reopened == payload
    assert object_ref.size == len(payload)


def test_walk_missing_parent_lineage_gap(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    ghost = ArtifactRef(artifact_id="art-ghost", sha256=GHOST_SHA256)
    receipt = publish(store, "art-a", b"payload-a", inputs=(ghost,))
    registry.register(store, receipt)

    with pytest.raises(RegistryError) as error:
        registry.walk(store, "art-a", depth=2)

    assert error.value.code == "lineage-gap"


def test_walk_parent_bytes_missing_lineage_gap(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    receipt_a = publish(store, "art-a", b"payload-a")
    receipt_b = publish(
        store,
        "art-b",
        b"payload-b",
        inputs=(ArtifactRef(artifact_id="art-a", sha256=receipt_a.content_sha256),),
    )
    registry.register(store, receipt_a)
    registry.register(store, receipt_b)
    object_file(tmp_path, receipt_a.content_sha256).unlink()

    with pytest.raises(RegistryError) as error:
        registry.walk(store, "art-b", depth=2)

    assert error.value.code == "lineage-gap"


def test_walk_cycle_detected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    receipt_a = publish(
        store,
        "art-a",
        b"payload-a",
        inputs=(ArtifactRef(artifact_id="art-b", sha256=sha256_bytes(b"payload-b")),),
    )
    receipt_b = publish(
        store,
        "art-b",
        b"payload-b",
        inputs=(ArtifactRef(artifact_id="art-a", sha256=sha256_bytes(b"payload-a")),),
    )
    registry.register(store, receipt_a)
    registry.register(store, receipt_b)

    with pytest.raises(RegistryError) as error:
        registry.walk(store, "art-a", depth=5)

    assert error.value.code == "lineage-cycle"


def test_walk_rejects_negative_depth(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)

    with pytest.raises(RegistryError) as error:
        registry.walk(store, "art-a", depth=-1)

    assert error.value.code == "walk-depth"
