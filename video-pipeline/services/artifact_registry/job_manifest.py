"""Job Manifest binding of stage lineage (inputs -> outputs).

A JobManifest pins the artifacts a stage consumed and produced. Validation
requires every bound ref to resolve in the registry index and to reopen
with verified bytes from the store; ``record_job`` then publishes the
manifest itself as an artifact and registers it, linking outputs."""

from __future__ import annotations

import hashlib

from services.artifact_registry.models import JOB_MANIFEST_SCHEMA, JobManifest
from services.artifact_registry.registry import ArtifactRegistry, RegistryError
from services.artifact_store.models import PublicationIntent, PublicationReceipt
from services.artifact_store.store import ArtifactStore, StoreRefusalError
from services.contracts.primitives import ArtifactEnvelope, Producer
from services.foundation_io import canonical_model_bytes

MANIFEST_ARTIFACT_TYPE = "job-manifest"
DEFAULT_RECORDER = Producer(name="job-manifest-recorder", version="v1")


def manifest_artifact_id(job_id: str) -> str:
    return f"job-manifest-{job_id}"


def validate_job_manifest(
    manifest: JobManifest,
    store: ArtifactStore,
    registry: ArtifactRegistry,
) -> None:
    index = registry.load()
    for ref in manifest.bound_refs():
        entry = index.entries.get(ref.artifact_id)
        if entry is None:
            raise RegistryError(
                "ref-unresolved",
                f"manifest ref {ref.artifact_id} is not in the registry index",
            )
        if entry.content_sha256 != ref.sha256:
            raise RegistryError(
                "ref-hash-mismatch",
                f"manifest ref {ref.artifact_id} hash {ref.sha256} "
                f"!= indexed {entry.content_sha256}",
            )
        try:
            store.reopen(ref.sha256)
        except StoreRefusalError as error:
            raise RegistryError(
                "ref-bytes-missing",
                f"manifest ref {ref.artifact_id} bytes are unavailable: {error}",
            ) from error


def record_job(
    store: ArtifactStore,
    registry: ArtifactRegistry,
    manifest: JobManifest,
    *,
    producer: Producer | None = None,
) -> PublicationReceipt:
    validate_job_manifest(manifest, store, registry)
    recorded = manifest.model_copy(update={"status": "recorded"})
    payload = canonical_model_bytes(recorded)
    envelope = ArtifactEnvelope(
        artifact_id=manifest_artifact_id(manifest.job_id),
        artifact_type=MANIFEST_ARTIFACT_TYPE,
        schema_version=JOB_MANIFEST_SCHEMA,
        content_hash=hashlib.sha256(payload).hexdigest(),
        producer=producer if producer is not None else DEFAULT_RECORDER,
        inputs=manifest.derivation_inputs(),
    )
    receipt = store.publish(PublicationIntent(envelope=envelope), payload)
    registry.register(store, receipt)
    return receipt
