"""The Todo-38 analyzer orchestration step for the Phase-1 fixture chain.

The real orchestrator machinery (cache keys over the edit-source world,
parallel execution, immutable publication, DuckDB index rebuild) runs over
the synthesized edit source with the declared-observation analyzer specs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.analyze.orchestrator import AnalyzeOrchestrator
from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.store import ArtifactStore
from services.cli.declared import declared_analysis, declared_specs, declared_transcript

if TYPE_CHECKING:
    from services.analyze.orchestrator_models import (
        AnalyzerArtifact,
        AnalyzerSpec,
        EditSourceRef,
        OrchestrationRecord,
    )
    from services.cli.media import SynthesizedMedia
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
    from services.toolchain.models import Phase1TechnicalToolchainLock


class AnalyzerStageError(Exception):
    """A declared analyzer failed inside the orchestration run."""


def run_analyzers(
    manifest: Phase1TechnicalFixtureManifest,
    lock: Phase1TechnicalToolchainLock,
    media: SynthesizedMedia,
    out_dir: Path,
) -> OrchestrationRecord:
    store = ArtifactStore(out_dir / "artifacts")
    registry = ArtifactRegistry(out_dir / "registry")
    orchestrator = AnalyzeOrchestrator(
        store=store,
        registry=registry,
        episode_dir=out_dir / "episode",
        state_root=out_dir / "episode" / "analyze-state",
        work_root=out_dir / "episode" / "analyze-work",
    )
    transcript = declared_transcript(manifest)
    analysis = declared_analysis(manifest)

    def runner(spec: AnalyzerSpec, edit_source: EditSourceRef, work_dir: Path) -> AnalyzerArtifact:
        del edit_source, work_dir
        if spec.analyzer_name == transcript.producer.name:
            return transcript
        if spec.analyzer_name == analysis.producer.name:
            return analysis
        raise ValueError(f"no declared analyzer for {spec.analyzer_name}")

    record = orchestrator.run_analyzers(media.edit_source, declared_specs(lock), runner)
    if record.failures:
        raise AnalyzerStageError(
            f"{record.failures[0].error_label}: {record.failures[0].detail}"
        )
    return record


__all__ = ["run_analyzers"]
