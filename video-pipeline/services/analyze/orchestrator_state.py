"""Durable orchestrator state: run bindings, supersession ledger, index view.

Three pieces, all Runtime State (never canonical artifacts):

- ``OrchestrationState``: the cache-key -> publication binding table. A key
  occupied by a binding is reused only when the binding recomputes exactly;
  a differing or unreadable binding is an explicit conflict, never a re-run.
- ``SupersessionLedger``: the registry ANNOTATION recording which published
  analyzer outputs were superseded (parent hash drift). Append-only audit
  history — artifacts are never deleted from the store or the registry.
- ``ActiveRegistryView``: a read-only registry view hiding superseded
  artifacts from FRESH index rebuilds; the exclusion itself is re-derived
  from the binding table each rebuild, so a re-adopted artifact becomes
  visible again without mutating history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from services.analyze.orchestrator_models import (
    AnalyzerSpec,
    EditSourceRef,
    OrchestratorCacheConflictError,
    edit_source_world_sha,
)
from services.artifact_registry.models import RegistryEntry, RegistryIndex, mint_index
from services.artifact_registry.registry import ArtifactRegistry, RegistryError
from services.contracts.primitives import ArtifactId, ArtifactRef, Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes

STATE_FILE_NAME = "orchestration-state.json"
SUPERSESSION_FILE_NAME = "superseded-artifacts.json"


class AnalyzerBinding(StrictModel):
    """What one cache key is bound to: spec, world, artifact, parents."""

    cache_key: Sha256
    spec: AnalyzerSpec
    edit_source_sha256: Sha256
    artifact_id: ArtifactId
    artifact_type: str
    content_sha256: Sha256
    parent_refs: tuple[ArtifactRef, ...] = ()


class _StateFile(StrictModel):
    schema_version: Literal["analyze-orchestration-v1"] = "analyze-orchestration-v1"
    bindings: dict[str, AnalyzerBinding] = Field(default_factory=dict)


class OrchestrationState:
    """Binding table on disk; single-writer (main thread), no eviction."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def path(self) -> Path:
        return self._root / STATE_FILE_NAME

    def bindings(self) -> dict[str, AnalyzerBinding]:
        if not self.path.is_file():
            return {}
        try:
            return _StateFile.model_validate_json(self.path.read_bytes()).bindings
        except (OSError, ValidationError) as error:
            raise OrchestratorCacheConflictError(
                f"orchestration state unreadable at {self.path}: {error}"
            ) from error

    def binding(self, cache_key: str) -> AnalyzerBinding | None:
        return self.bindings().get(cache_key)

    def record(self, binding: AnalyzerBinding) -> None:
        current = self.bindings()
        existing = current.get(binding.cache_key)
        if existing is not None:
            if existing != binding:
                raise OrchestratorCacheConflictError(
                    f"cache key {binding.cache_key[:12]} already bound to a different "
                    "publication; divergent duplicate completion refused"
                )
            return
        self._root.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self.path,
            canonical_model_bytes(
                _StateFile(bindings=current | {binding.cache_key: binding})
            ),
        )


class SupersededEntry(StrictModel):
    """Audit annotation for one superseded published output."""

    content_sha256: Sha256
    reason: Literal["parent-hash-drift"]
    detail: str


class _LedgerFile(StrictModel):
    schema_version: Literal["analyze-supersession-v1"] = "analyze-supersession-v1"
    entries: dict[ArtifactId, SupersededEntry] = Field(default_factory=dict)


class SupersessionLedger:
    """Registry annotation file next to the registry index; append-only."""

    def __init__(self, registry_root: Path) -> None:
        self._path = registry_root / SUPERSESSION_FILE_NAME

    def entries(self) -> dict[str, SupersededEntry]:
        if not self._path.is_file():
            return {}
        try:
            return dict(_LedgerFile.model_validate_json(self._path.read_bytes()).entries)
        except (OSError, ValidationError) as error:
            raise OrchestratorCacheConflictError(
                f"supersession ledger unreadable at {self._path}: {error}"
            ) from error

    def annotate(self, binding: AnalyzerBinding, *, detail: str) -> bool:
        """Record one supersession; True when newly added. Never removes."""

        current = self.entries()
        existing = current.get(binding.artifact_id)
        entry = SupersededEntry(
            content_sha256=binding.content_sha256,
            reason="parent-hash-drift",
            detail=detail,
        )
        if existing is not None:
            if existing.content_sha256 != binding.content_sha256:
                raise OrchestratorCacheConflictError(
                    f"supersession ledger holds {binding.artifact_id} with different "
                    "content; annotation refused"
                )
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            self._path,
            canonical_model_bytes(
                _LedgerFile(entries=current | {binding.artifact_id: entry})
            ),
        )
        return True


def superseded_binding_ids(
    bindings: dict[str, AnalyzerBinding],
    *,
    edit_source: EditSourceRef,
    current_keys: frozenset[str],
    adopted_names: frozenset[str],
) -> frozenset[str]:
    """Artifact ids whose parents no longer match the current committed world.

    A binding stays current when its key was refreshed this run; otherwise it
    is superseded when its edit-source world drifted, or when the same
    analyzer has since adopted a newer output under different parameters.
    """

    world = edit_source_world_sha(edit_source)
    return frozenset(
        binding.artifact_id
        for key, binding in bindings.items()
        if key not in current_keys
        and (binding.edit_source_sha256 != world or binding.spec.analyzer_name in adopted_names)
    )


class ActiveRegistryView(ArtifactRegistry):
    """Read view of the real registry hiding excluded (superseded) entries.

    The underlying registry index is NEVER rewritten through this view: any
    write path raises. Lineage audit history stays complete in the real
    registry; only fresh index rebuilds see the active subset.
    """

    def __init__(self, registry: ArtifactRegistry, excluded: frozenset[str]) -> None:
        super().__init__(registry.index_root)
        self._excluded = excluded

    def load(self) -> RegistryIndex:
        full = super().load()
        active: dict[str, RegistryEntry] = {
            artifact_id: entry
            for artifact_id, entry in full.entries.items()
            if artifact_id not in self._excluded
        }
        return mint_index(active)

    def save(self, index: RegistryIndex) -> None:  # noqa: ARG002 (override signature)
        raise RegistryError(
            "registry-view-readonly",
            "the active registry view must never write the registry index",
        )


__all__ = [
    "STATE_FILE_NAME",
    "SUPERSESSION_FILE_NAME",
    "ActiveRegistryView",
    "AnalyzerBinding",
    "OrchestrationState",
    "SupersededEntry",
    "SupersessionLedger",
    "superseded_binding_ids",
]
