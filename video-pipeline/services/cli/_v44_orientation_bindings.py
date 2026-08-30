"""T9 finishing-run wiring for the orientation QC bindings.

The finishing path owns the episode-layout knowledge (committed Source
Manifest under ``run/``, the hash-verified mezzanine from the review
bundle, and the committed 0C-flavor IR), so it assembles the
:class:`services.qc.inputs.OptionalBindings` the QC engine needs for the
upright-frame orientation observation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli.episode_runner_workspace import RUN_DIR_NAME
from services.contracts.timeline_ir import TimelineIrProduction, TimelineTrackProduction
from services.foundation_io import atomic_write, canonical_model_bytes
from services.qc.inputs import OptionalBindings

if TYPE_CHECKING:
    from services.cli._v44_finishing_build import EpisodeContext
    from services.contracts.timeline_ir import TimelineIr0C

SOURCE_MANIFEST_NAME: Final = "source-manifest.json"


def production_ir_for_qc(ir: TimelineIr0C) -> TimelineIrProduction:
    """Re-envelope the committed 0C IR as the production IR the QC binding
    parses (same tracks/items; the editorial content hash is carried over)."""
    return TimelineIrProduction(
        artifact_id=ir.artifact_id,
        artifact_type="timeline_ir_v1",
        schema_version="timeline-ir-v1",
        content_hash=ir.content_hash,
        producer=ir.producer,
        inputs=ir.inputs,
        rate=ir.rate,
        tracks=tuple(
            TimelineTrackProduction(track=track.track, items=track.items)
            for track in ir.tracks
        ),
    )


def orientation_bindings(
    finishing_dir: Path, episode_root: Path, ctx: EpisodeContext
) -> OptionalBindings:
    """Bind the orientation-QC inputs: committed Source Manifest, verified
    edit source (mezzanine), and the re-serialized production IR."""
    ir_path = finishing_dir / "timeline-ir-qc.json"
    atomic_write(ir_path, canonical_model_bytes(production_ir_for_qc(ctx.ir)))
    manifest = episode_root / RUN_DIR_NAME / SOURCE_MANIFEST_NAME
    return OptionalBindings(
        ir=ir_path,
        source_manifest=manifest if manifest.is_file() else None,
        edit_source=ctx.mezzanine if ctx.mezzanine.is_file() else None,
    )


__all__ = ["orientation_bindings", "production_ir_for_qc"]
