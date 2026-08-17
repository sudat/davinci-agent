from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.artifact_registry.job_manifest import record_job, validate_job_manifest
from services.artifact_registry.models import JobManifest
from services.artifact_registry.registry import RegistryError
from services.contracts.primitives import ArtifactRef
from tests.artifact_registry.support import (
    GHOST_SHA256,
    make_registry,
    make_store,
    object_file,
    publish,
    ref_for,
)

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.models import PublicationReceipt
    from services.artifact_store.store import ArtifactStore


def _published_refs(
    store: ArtifactStore, registry: ArtifactRegistry
) -> dict[str, ArtifactRef]:
    receipts: dict[str, PublicationReceipt] = {
        "cfg": publish(store, "cfg-snapshot", b"config-bytes"),
        "src": publish(store, "src-manifest", b"source-bytes"),
        "tool": publish(store, "toolchain-lock", b"toolchain-bytes"),
    }
    for receipt in receipts.values():
        registry.register(store, receipt)
    return {name: ref_for(receipt) for name, receipt in receipts.items()}


def _manifest(refs: dict[str, ArtifactRef]) -> JobManifest:
    return JobManifest(
        job_id="job-0001",
        episode_id="ep-0001",
        stage="ANALYZED",
        input_refs=(refs["src"],),
        resolved_config_ref=refs["cfg"],
        toolchain_ref=refs["tool"],
        output_refs=(refs.get("out", refs["src"]),),
    )


def test_record_job_publishes_and_registers_manifest(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    refs = _published_refs(store, registry)
    output_receipt = publish(store, "qc-report", b"output-bytes")
    registry.register(store, output_receipt)
    refs["out"] = ref_for(output_receipt)
    manifest = _manifest(refs)

    validate_job_manifest(manifest, store, registry)
    receipt = record_job(store, registry, manifest)

    assert receipt.artifact_id == "job-manifest-job-0001"
    payload, _ref = store.reopen(receipt.content_sha256)
    recorded = JobManifest.model_validate_json(payload)
    assert recorded.status == "recorded"
    assert recorded.output_refs == manifest.output_refs

    index = registry.load()
    manifest_entry = index.entries["job-manifest-job-0001"]
    assert manifest_entry.artifact_type == "job-manifest"
    assert manifest_entry.content_sha256 == receipt.content_sha256
    parents = {ref.artifact_id for ref in manifest_entry.inputs}
    assert parents == {"src-manifest", "cfg-snapshot", "toolchain-lock"}

    nodes = registry.walk(store, "job-manifest-job-0001", depth=2)
    walked = {node.entry.artifact_id for node in nodes}
    assert {
        "job-manifest-job-0001",
        "src-manifest",
        "cfg-snapshot",
        "toolchain-lock",
    } <= walked


def test_record_job_is_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    refs = _published_refs(store, registry)
    manifest = _manifest(refs)

    first = record_job(store, registry, manifest)
    second = record_job(store, registry, manifest)

    assert first.content_sha256 == second.content_sha256
    assert second.idempotent is True


def test_validate_ref_unresolved(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    refs = _published_refs(store, registry)
    refs["src"] = ArtifactRef(artifact_id="art-ghost", sha256=GHOST_SHA256)
    refs["out"] = refs["cfg"]
    manifest = _manifest(refs)

    with pytest.raises(RegistryError) as error:
        validate_job_manifest(manifest, store, registry)

    assert error.value.code == "ref-unresolved"


def test_validate_ref_hash_mismatch(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    refs = _published_refs(store, registry)
    refs["src"] = ArtifactRef(artifact_id="src-manifest", sha256=GHOST_SHA256)
    refs["out"] = refs["cfg"]
    manifest = _manifest(refs)

    with pytest.raises(RegistryError) as error:
        validate_job_manifest(manifest, store, registry)

    assert error.value.code == "ref-hash-mismatch"


def test_record_job_refuses_missing_bytes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    registry = make_registry(tmp_path)
    refs = _published_refs(store, registry)
    refs["out"] = refs["cfg"]
    manifest = _manifest(refs)
    object_file(tmp_path, refs["src"].sha256).unlink()

    with pytest.raises(RegistryError) as error:
        record_job(store, registry, manifest)

    assert error.value.code == "ref-bytes-missing"
    assert "job-manifest-job-0001" not in registry.load().entries
