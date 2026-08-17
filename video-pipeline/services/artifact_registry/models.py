"""Strict models for the local artifact lineage registry.

The registry is a derived local index over the immutable artifact store:
the store keeps the truth (content-addressed objects plus meta sidecars),
while the registry only indexes published artifacts for lineage queries.
The index file is sealed with a content hash recomputed from its canonical
bytes, so any unsealed edit is rejected instead of trusted."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    ArtifactId,
    ArtifactRef,
    Identifier,
    Producer,
    SchemaVersion,
    Sha256,
    StrictModel,
)
from services.foundation_io import canonical_model_bytes

REGISTRY_INDEX_SCHEMA = "registry-index-v1"
JOB_MANIFEST_SCHEMA = "job-manifest-v1"

ManifestStatus = Literal["draft", "recorded"]


class RegistryEntry(StrictModel):
    artifact_id: ArtifactId
    artifact_type: Identifier
    schema_version: SchemaVersion
    content_sha256: Sha256
    size: int = Field(ge=0, strict=True)
    producer: Producer
    inputs: tuple[ArtifactRef, ...] = ()
    sequence: int = Field(ge=0, strict=True)


class RegistryIndexBody(StrictModel):
    schema_version: Literal["registry-index-v1"]
    entries: dict[ArtifactId, RegistryEntry] = Field(default_factory=dict)
    by_sha256: dict[Sha256, tuple[ArtifactId, ...]] = Field(default_factory=dict)


class RegistryIndex(StrictModel):
    schema_version: Literal["registry-index-v1"] = "registry-index-v1"
    entries: dict[ArtifactId, RegistryEntry] = Field(default_factory=dict)
    by_sha256: dict[Sha256, tuple[ArtifactId, ...]] = Field(default_factory=dict)
    index_sha256: Sha256

    @model_validator(mode="after")
    def require_sealed_and_consistent(self) -> RegistryIndex:
        body = RegistryIndexBody(
            schema_version=self.schema_version,
            entries=self.entries,
            by_sha256=self.by_sha256,
        )
        seal = hashlib.sha256(canonical_model_bytes(body)).hexdigest()
        if seal != self.index_sha256:
            raise PydanticCustomError(
                "index_seal",
                "index seal mismatch: registry index bytes are not trustworthy",
            )
        expected: dict[str, set[str]] = {}
        for artifact_id, entry in self.entries.items():
            if entry.artifact_id != artifact_id:
                raise PydanticCustomError(
                    "index_entry_key",
                    "entry key {key} must equal the entry artifact_id",
                    {"key": artifact_id},
                )
            expected.setdefault(entry.content_sha256, set()).add(artifact_id)
        for content_sha256, artifact_ids in self.by_sha256.items():
            if set(artifact_ids) != expected.get(content_sha256, set()):
                raise PydanticCustomError(
                    "index_by_sha256",
                    "by_sha256 mapping disagrees with entries for {hash}",
                    {"hash": content_sha256},
                )
        if set(self.by_sha256) != set(expected):
            raise PydanticCustomError(
                "index_by_sha256",
                "by_sha256 keys must cover exactly the entry content hashes",
            )
        return self


def index_seal(
    entries: Mapping[str, RegistryEntry],
    by_sha256: Mapping[str, tuple[str, ...]],
) -> str:
    body = RegistryIndexBody(
        schema_version=REGISTRY_INDEX_SCHEMA,
        entries=dict(entries),
        by_sha256=dict(by_sha256),
    )
    return hashlib.sha256(canonical_model_bytes(body)).hexdigest()


def mint_index(entries: Mapping[str, RegistryEntry]) -> RegistryIndex:
    ordered = sorted(entries.values(), key=lambda entry: (entry.sequence, entry.artifact_id))
    by_sha256: dict[str, tuple[str, ...]] = {}
    for entry in ordered:
        artifact_ids = [*by_sha256.get(entry.content_sha256, ()), entry.artifact_id]
        by_sha256[entry.content_sha256] = tuple(artifact_ids)
    body_entries = {entry.artifact_id: entry for entry in ordered}
    return RegistryIndex(
        schema_version=REGISTRY_INDEX_SCHEMA,
        entries=body_entries,
        by_sha256=by_sha256,
        index_sha256=index_seal(body_entries, by_sha256),
    )


class JobManifest(StrictModel):
    schema_version: Literal["job-manifest-v1"] = "job-manifest-v1"
    job_id: Identifier
    episode_id: Identifier
    stage: Identifier
    input_refs: tuple[ArtifactRef, ...] = ()
    resolved_config_ref: ArtifactRef
    toolchain_ref: ArtifactRef
    model_refs: tuple[ArtifactRef, ...] = ()
    request_refs: tuple[ArtifactRef, ...] = ()
    output_refs: tuple[ArtifactRef, ...] = ()
    status: ManifestStatus = "draft"

    def derivation_inputs(self) -> tuple[ArtifactRef, ...]:
        return (
            *self.input_refs,
            self.resolved_config_ref,
            self.toolchain_ref,
            *self.model_refs,
            *self.request_refs,
        )

    def bound_refs(self) -> tuple[ArtifactRef, ...]:
        return (*self.derivation_inputs(), *self.output_refs)
