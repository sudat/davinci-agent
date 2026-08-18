"""Review-plane preview rendering: SRT table, per-kind bindings, render.

The frozen 0C plan contract binds every item to the single edit-source id,
so media binding is done per item KIND (A/V items one synthesized mezzanine,
subtitle items one generated SRT table) rather than per source id.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.cli.project import ProjectionError
from services.foundation_io import atomic_write, sha256_file
from services.preview.models import (
    AppliedDecision,
    ItemBinding,
    MediaBinding,
    PreviewMediaBindings,
    PreviewTraceManifest,
)
from services.preview.render import render_preview
from services.preview.srt import expected_subtitle_cues, render_srt

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C
    from services.preview.tools import PinnedTools


def _subtitle_table(ir: TimelineIr0C, out_dir: Path) -> Path:
    cues = expected_subtitle_cues(
        tuple(
            item for track in ir.tracks if track.track.kind == "subtitle" for item in track.items
        ),
        ir.rate,
    )
    path = out_dir / "subtitle-table.srt"
    atomic_write(path, render_srt(cues))
    return path


def _bindings(ir: TimelineIr0C, mezzanine: Path, media_dir: Path) -> PreviewMediaBindings:
    subtitle_items = tuple(
        item for track in ir.tracks if track.track.kind == "subtitle" for item in track.items
    )
    table = _subtitle_table(ir, media_dir) if subtitle_items else None
    items: list[ItemBinding] = []
    for track in ir.tracks:
        for item in track.items:
            media = table if item.kind == "subtitle" else mezzanine
            if media is None:
                raise ProjectionError(
                    "binding_missing", f"subtitle item {item.item_id} has no table to bind"
                )
            items.append(
                ItemBinding(
                    item_id=item.item_id,
                    binding=MediaBinding(media_path=str(media), sha256=sha256_file(media)),
                )
            )
    return PreviewMediaBindings(items=tuple(items), bgm=None)


def render_review_preview(  # noqa: PLR0913 (preview adapter contract: plan/IR/media/out + tools)
    plan: EditPlan0C,
    ir: TimelineIr0C,
    mezzanine: Path,
    out_dir: Path,
    *,
    tools: PinnedTools,
    decision: AppliedDecision | None = None,
) -> PreviewTraceManifest:
    """Render the review-plane preview bound to the synthesized mezzanine."""

    out_dir.mkdir(parents=True, exist_ok=True)
    bindings = _bindings(ir, mezzanine, out_dir.parent / "media")
    return render_preview(plan, ir, bindings, out_dir, tools=tools, decision=decision)


__all__ = ["render_review_preview"]
