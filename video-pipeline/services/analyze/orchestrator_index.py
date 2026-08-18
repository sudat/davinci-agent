"""Post-publish finalization: supersession sweep and index reconciliation.

The sweep annotates (never deletes) published outputs whose parents drifted
from the committed world; the rebuild regenerates the episode's media-query
index from ACTIVE evidence only, excluding exactly the derived superseded
set — so a re-adopted artifact becomes visible again without rewriting
audit history, and every fresh index excludes stale rows by construction.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.analyze.orchestrator_models import (
    FAULT_BEFORE_INDEX,
    EditSourceRef,
    FaultHook,
)
from services.analyze.orchestrator_state import (
    ActiveRegistryView,
    OrchestrationState,
    SupersessionLedger,
    superseded_binding_ids,
)
from services.contracts.primitives import ArtifactRef
from services.media_query.index import MEDIA_DB_NAME, MediaQueryIndex

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore


def sweep_superseded(
    *,
    ledger: SupersessionLedger,
    state: OrchestrationState,
    edit_source: EditSourceRef,
    current_keys: frozenset[str],
    adopted_names: frozenset[str],
) -> tuple[ArtifactRef, ...]:
    bindings = state.bindings()
    stale = superseded_binding_ids(
        bindings,
        edit_source=edit_source,
        current_keys=current_keys,
        adopted_names=adopted_names,
    )
    superseded: list[ArtifactRef] = []
    for binding in sorted(
        (item for item in bindings.values() if item.artifact_id in stale),
        key=lambda item: item.artifact_id,
    ):
        ledger.annotate(
            binding,
            detail=(
                f"parents drifted from the committed world; "
                f"recorded world {binding.edit_source_sha256[:12]}"
            ),
        )
        superseded.append(
            ArtifactRef(artifact_id=binding.artifact_id, sha256=binding.content_sha256)
        )
    return tuple(superseded)


def rebuild_episode_index(  # noqa: PLR0913 (authority wiring mirrors the sweep contract)
    *,
    store: ArtifactStore,
    registry: ArtifactRegistry,
    episode_dir: Path,
    state: OrchestrationState,
    edit_source: EditSourceRef,
    current_keys: frozenset[str],
    adopted_names: frozenset[str],
    fault_hook: FaultHook | None,
) -> Path:
    if fault_hook is not None:
        fault_hook(FAULT_BEFORE_INDEX)
    excluded = superseded_binding_ids(
        state.bindings(),
        edit_source=edit_source,
        current_keys=current_keys,
        adopted_names=adopted_names,
    )
    episode_dir.mkdir(parents=True, exist_ok=True)
    path = episode_dir / MEDIA_DB_NAME
    index = MediaQueryIndex.open(path)
    try:
        index.rebuild(store, ActiveRegistryView(registry, excluded), episode_dir)
    finally:
        index.close()
    return path


__all__ = ["rebuild_episode_index", "sweep_superseded"]
