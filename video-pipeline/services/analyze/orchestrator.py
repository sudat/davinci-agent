"""The analyzer orchestrator (Todo 38): cached, parallel, immutable evidence.

``run_analyzers`` resolves every spec against the content-addressed binding
cache (HIT = byte-verified store reopen, never a re-run), executes the misses
concurrently over read-only inputs and per-spec work dirs (frozen max
workers), then adopts sequentially through the publication gate, sweeps
SUPERSEDED outputs (never deleted), and rebuilds the media-query index from
active evidence only. Analyzer completion never advances Plans: this layer
touches only the store, the registry, and the rebuildable index — plan
mutation lanes live elsewhere by contract.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.analyze.analysis_models import AnalyzeError
from services.analyze.orchestrator_adopt import adopt_result
from services.analyze.orchestrator_cache import resolve_cache_hit
from services.analyze.orchestrator_index import rebuild_episode_index, sweep_superseded
from services.analyze.orchestrator_models import (
    FAULT_BEFORE_INDEX,
    FAULT_BEFORE_PUBLISH,
    AnalyzerArtifact,
    AnalyzerFailure,
    AnalyzerSpec,
    EditSourceRef,
    OrchestrationRecord,
    OrchestrationResult,
    orchestration_cache_key,
    verify_edit_source,
)
from services.analyze.orchestrator_state import OrchestrationState, SupersessionLedger

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore

MAX_WORKERS: Final = 4

AnalyzerRunner = Callable[[AnalyzerSpec, EditSourceRef, Path], AnalyzerArtifact]
Execution = AnalyzerArtifact | AnalyzerFailure


class AnalyzeOrchestrator:
    """Runs analyzer specs cached, in parallel, and immutable-publication-safe."""

    def __init__(  # noqa: PLR0913 (authority wiring is the constructor)
        self,
        *,
        store: ArtifactStore,
        registry: ArtifactRegistry,
        episode_dir: Path,
        state_root: Path,
        work_root: Path,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._episode_dir = episode_dir
        self._state = OrchestrationState(state_root)
        self._ledger = SupersessionLedger(registry.index_root)
        self._work_root = work_root
        self._fault_hook = fault_hook

    def run_analyzers(
        self,
        edit_source: EditSourceRef,
        specs: tuple[AnalyzerSpec, ...],
        runner: AnalyzerRunner,
    ) -> OrchestrationRecord:
        world = verify_edit_source(edit_source)
        keys = tuple(
            orchestration_cache_key(edit_source_sha256=world, spec=spec) for spec in specs
        )
        results: list[OrchestrationResult] = []
        pending: list[tuple[str, AnalyzerSpec]] = []
        for spec, key in zip(specs, keys, strict=True):
            hit = resolve_cache_hit(
                store=self._store,
                registry=self._registry,
                state=self._state,
                key=key,
                spec=spec,
                world=world,
            )
            if hit is None:
                pending.append((key, spec))
            else:
                results.append(hit)

        executed = self._execute_pending(edit_source, pending, runner)
        for (key, spec), outcome in zip(pending, executed, strict=True):
            if not isinstance(outcome, AnalyzerFailure):
                results.append(
                    adopt_result(
                        store=self._store,
                        registry=self._registry,
                        state=self._state,
                        fault_hook=self._fault_hook,
                        edit_source=edit_source,
                        key=key,
                        spec=spec,
                        artifact=outcome,
                        world=world,
                    )
                )
        failures = tuple(item for item in executed if isinstance(item, AnalyzerFailure))

        current_keys = frozenset(keys)
        adopted_names = frozenset(item.spec.analyzer_name for item in results if item.artifact)
        superseded = sweep_superseded(
            ledger=self._ledger,
            state=self._state,
            edit_source=edit_source,
            current_keys=current_keys,
            adopted_names=adopted_names,
        )
        index_path = rebuild_episode_index(
            store=self._store,
            registry=self._registry,
            episode_dir=self._episode_dir,
            state=self._state,
            edit_source=edit_source,
            current_keys=current_keys,
            adopted_names=adopted_names,
            fault_hook=self._fault_hook,
        )
        return OrchestrationRecord(
            edit_source_sha256=world,
            results=tuple(results),
            failures=failures,
            superseded_artifacts=superseded,
            index_path=str(index_path),
        )

    def _execute_pending(
        self,
        edit_source: EditSourceRef,
        pending: list[tuple[str, AnalyzerSpec]],
        runner: AnalyzerRunner,
    ) -> list[Execution]:
        if not pending:
            return []

        def run_one(position: int, key: str, spec: AnalyzerSpec) -> Execution:
            work_dir = self._work_root / f"{position:03d}-{spec.analyzer_name}-{key[:12]}"
            try:
                return runner(spec, edit_source, work_dir)
            except AnalyzeError as error:
                return AnalyzerFailure(spec=spec, error_label=error.label, detail=error.detail)
            except OSError as error:
                label = "analyzer_io_error"
                return AnalyzerFailure(spec=spec, error_label=label, detail=str(error))
            except Exception as error:  # noqa: BLE001 (isolation: one analyzer must not kill the run)
                return AnalyzerFailure(
                    spec=spec,
                    error_label="analyzer_failed",
                    detail=f"{type(error).__name__}: {error}",
                )

        with ThreadPoolExecutor(
            max_workers=MAX_WORKERS, thread_name_prefix="analyze-orchestrator"
        ) as pool:
            return list(
                pool.map(
                    run_one,
                    range(len(pending)),
                    (key for key, _spec in pending),
                    (spec for _key, spec in pending),
                )
            )


__all__ = [
    "FAULT_BEFORE_INDEX",
    "FAULT_BEFORE_PUBLISH",
    "MAX_WORKERS",
    "AnalyzeOrchestrator",
    "AnalyzerRunner",
]
