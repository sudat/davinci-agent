"""Sequential adoption of completed analyzer results (Todo 38).

Adoption is the immutable-publication gate: the committed edit-source world
is re-verified BEFORE each publish (a parent that drifted between completion
and publish supersedes the result instead of adopting it), the artifact must
come from the spec's own analyzer identity and bind only declared
edit-source parents, and only then are canonical bytes published through the
content-addressed store, the receipt registered, and the binding recorded —
the crash window between publish and record safely re-runs.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

from services.analyze.orchestrator_cache import register_receipt
from services.analyze.orchestrator_models import (
    FAULT_BEFORE_PUBLISH,
    AnalyzerArtifact,
    AnalyzerSpec,
    EditSourceRef,
    FaultHook,
    OrchestrationResult,
    OrchestratorError,
    verify_edit_source,
)
from services.analyze.orchestrator_state import AnalyzerBinding, OrchestrationState
from services.artifact_store.models import PublicationIntent
from services.artifact_store.store import StoreRefusalError
from services.contracts.primitives import ArtifactEnvelope, ArtifactRef
from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore

EDIT_SOURCE_PARENT_PREFIX: Final = "edit-source"
UNREADABLE_WORLD: Final = "<unreadable-edit-source>"


def world_sha_quiet(edit_source: EditSourceRef) -> str:
    try:
        return verify_edit_source(edit_source)
    except (OSError, OrchestratorError):
        return UNREADABLE_WORLD


def _require_declared_parents(
    edit_source: EditSourceRef, spec: AnalyzerSpec, artifact: AnalyzerArtifact
) -> None:
    declared = {item.sha256 for item in edit_source.files}
    for parent in artifact.inputs:
        foreign = parent.artifact_id.startswith(EDIT_SOURCE_PARENT_PREFIX) and (
            parent.sha256 not in declared
        )
        if foreign:
            raise OrchestratorError(
                "foreign_edit_source_parent",
                f"{spec.analyzer_name} bound parent {parent.artifact_id} with hash "
                f"{parent.sha256[:12]} that is not a declared edit-source file",
            )


def adopt_result(  # noqa: PLR0913 (the re-check chain IS the adoption gate)
    *,
    store: ArtifactStore,
    registry: ArtifactRegistry,
    state: OrchestrationState,
    fault_hook: FaultHook | None,
    edit_source: EditSourceRef,
    key: str,
    spec: AnalyzerSpec,
    artifact: AnalyzerArtifact,
    world: str,
) -> OrchestrationResult:
    if fault_hook is not None:
        fault_hook(FAULT_BEFORE_PUBLISH)
    if world_sha_quiet(edit_source) != world:
        # parent drifted between completion and publish: stale, not adopted
        return OrchestrationResult(
            spec=spec, cache_status="miss", artifact=None, superseded=True
        )
    if (
        artifact.producer.name != spec.analyzer_name
        or artifact.producer.version != spec.analyzer_version
    ):
        raise OrchestratorError(
            "spec_artifact_mismatch",
            f"runner returned {artifact.producer.name}/{artifact.producer.version} "
            f"for spec {spec.analyzer_name}/{spec.analyzer_version}",
        )
    _require_declared_parents(edit_source, spec, artifact)
    payload = canonical_model_bytes(artifact)
    content_sha256 = hashlib.sha256(payload).hexdigest()
    envelope = ArtifactEnvelope(
        artifact_id=artifact.artifact_id,
        artifact_type=artifact.artifact_type,
        schema_version=artifact.schema_version,
        content_hash=content_sha256,
        producer=artifact.producer,
        inputs=artifact.inputs,
    )
    try:
        receipt = store.publish(PublicationIntent(envelope=envelope), payload)
    except StoreRefusalError as error:
        raise OrchestratorError(
            "publish_refused",
            f"publishing {spec.analyzer_name} failed: {error.code} {error.detail}",
        ) from error
    register_receipt(store, registry, receipt)
    state.record(
        AnalyzerBinding(
            cache_key=key,
            spec=spec,
            edit_source_sha256=world,
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type,
            content_sha256=content_sha256,
            parent_refs=artifact.inputs,
        )
    )
    return OrchestrationResult(
        spec=spec,
        cache_status="miss",
        artifact=ArtifactRef(artifact_id=artifact.artifact_id, sha256=content_sha256),
        publish_idempotent=receipt.idempotent,
    )


__all__ = ["adopt_result", "world_sha_quiet"]
