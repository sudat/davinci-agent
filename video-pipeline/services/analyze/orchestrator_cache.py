"""Cache-hit resolution for the analyzer orchestrator (Todo 38).

A HIT is only ever a byte-verified store reopen of the bound artifact whose
payload re-parses as the declared indexable evidence type and whose envelope
agrees with the binding record; the registry entry is re-asserted so a crash
between store publish and registry register recovers as a HIT. Any
disagreement — foreign binding, unreadable bytes, drift — is an explicit
conflict: never silently reused, never silently re-run around.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from services.analyze.orchestrator_models import (
    AnalyzerSpec,
    OrchestrationResult,
    OrchestratorCacheConflictError,
    OrchestratorError,
)
from services.artifact_registry.registry import ArtifactRegistry, RegistryError
from services.artifact_store.models import PublicationReceipt
from services.artifact_store.store import ArtifactStore, StoreRefusalError
from services.contracts.primitives import ArtifactRef
from services.media_query.index import INDEXABLE_TYPES

if TYPE_CHECKING:
    from services.analyze.orchestrator_state import OrchestrationState


def register_receipt(
    store: ArtifactStore, registry: ArtifactRegistry, receipt: PublicationReceipt
) -> None:
    try:
        registry.register(store, receipt)
    except RegistryError as error:
        raise OrchestratorError(
            "registry_conflict",
            f"registering {receipt.artifact_id} failed: {error.code} {error.detail}",
        ) from error


def resolve_cache_hit(  # noqa: PLR0913 (verification chain is one atomic resolution)
    *,
    store: ArtifactStore,
    registry: ArtifactRegistry,
    state: OrchestrationState,
    key: str,
    spec: AnalyzerSpec,
    world: str,
) -> OrchestrationResult | None:
    binding = state.binding(key)
    if binding is None:
        return None
    if binding.spec != spec or binding.edit_source_sha256 != world:
        raise OrchestratorCacheConflictError(
            f"cache key {key[:12]} is bound to a different spec or edit source; "
            "refusing overwrite or silent reuse"
        )
    try:
        payload, object_ref = store.reopen(binding.content_sha256)
    except StoreRefusalError as error:
        raise OrchestratorCacheConflictError(
            f"bound artifact for {key[:12]} failed store verification: {error.code}"
        ) from error
    model = INDEXABLE_TYPES.get(binding.artifact_type)
    if model is None:
        raise OrchestratorCacheConflictError(
            f"bound artifact type {binding.artifact_type} is not indexable evidence"
        )
    try:
        artifact = model.model_validate_json(payload)
    except ValidationError as error:
        raise OrchestratorCacheConflictError(
            f"bound payload for {key[:12]} no longer parses: {error}"
        ) from error
    if (
        artifact.artifact_id != binding.artifact_id
        or artifact.producer.name != spec.analyzer_name
        or artifact.producer.version != spec.analyzer_version
    ):
        raise OrchestratorCacheConflictError(
            f"bound payload for {key[:12]} disagrees with its binding record"
        )
    register_receipt(
        store,
        registry,
        PublicationReceipt(
            artifact_id=binding.artifact_id,
            content_sha256=binding.content_sha256,
            object_ref=object_ref,
            meta_relative_path=f"artifacts/{binding.artifact_id}.json",
            idempotent=True,
        ),
    )
    return OrchestrationResult(
        spec=spec,
        cache_status="hit",
        artifact=ArtifactRef(artifact_id=binding.artifact_id, sha256=binding.content_sha256),
    )


__all__ = ["register_receipt", "resolve_cache_hit"]
