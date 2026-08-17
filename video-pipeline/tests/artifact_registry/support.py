from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import ValidationError

from services.artifact_registry.reconcile import (
    RebuildJournalEvent,
    ReconcileJournalEvent,
)
from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.models import PublicationIntent, PublicationReceipt
from services.artifact_store.store import ArtifactStore
from services.contracts.primitives import (
    ArtifactEnvelope,
    ArtifactRef,
    Producer,
)

DEFAULT_PRODUCER = Producer(name="registry-test-producer", version="v1")
GHOST_SHA256 = "0" * 64


def make_store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "store")


def make_registry(tmp_path: Path) -> ArtifactRegistry:
    return ArtifactRegistry(tmp_path / "registry")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def publish(
    store: ArtifactStore,
    artifact_id: str,
    payload: bytes,
    *,
    inputs: tuple[ArtifactRef, ...] = (),
    producer: Producer = DEFAULT_PRODUCER,
) -> PublicationReceipt:
    intent = _intent_for(artifact_id, payload, inputs=inputs, producer=producer)
    return store.publish(intent, payload)


def publish_duplicate_id(
    store: ArtifactStore,
    artifact_id: str,
    payload: bytes,
    *,
    producer: Producer = DEFAULT_PRODUCER,
) -> PublicationReceipt:
    """Publish the same bytes under a second artifact id.

    The store journal is keyed by content hash, so the second publication
    reuses the already-stored object and completes via the meta sidecar."""

    _payload, object_ref = store.reopen(sha256_bytes(payload))
    intent = _intent_for(artifact_id, payload, producer=producer)
    store.write_meta(intent)
    return PublicationReceipt(
        artifact_id=artifact_id,
        content_sha256=object_ref.sha256,
        object_ref=object_ref,
        meta_relative_path=f"artifacts/{artifact_id}.json",
        idempotent=True,
    )


def _intent_for(
    artifact_id: str,
    payload: bytes,
    *,
    inputs: tuple[ArtifactRef, ...] = (),
    producer: Producer = DEFAULT_PRODUCER,
) -> PublicationIntent:
    return PublicationIntent(
        envelope=ArtifactEnvelope(
            artifact_id=artifact_id,
            artifact_type="registry-test-artifact",
            schema_version="registry-test-v1",
            content_hash=sha256_bytes(payload),
            producer=producer,
            inputs=inputs,
        )
    )


def ref_for(receipt: PublicationReceipt) -> ArtifactRef:
    return ArtifactRef(artifact_id=receipt.artifact_id, sha256=receipt.content_sha256)


def object_file(tmp_path: Path, content_sha256: str) -> Path:
    return tmp_path / "store" / "objects" / content_sha256[:2] / content_sha256


def journal_events(
    registry_root: Path,
) -> list[ReconcileJournalEvent | RebuildJournalEvent]:
    path = registry_root / "registry-journal.jsonl"
    events: list[ReconcileJournalEvent | RebuildJournalEvent] = []
    for line in path.read_text().splitlines():
        if not line:
            continue
        try:
            events.append(ReconcileJournalEvent.model_validate_json(line))
        except ValidationError:
            events.append(RebuildJournalEvent.model_validate_json(line))
    return events
