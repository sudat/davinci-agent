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
    PresentationRenderSettings,
    PreviewMediaBindings,
    PreviewTraceManifest,
    TracePresentation,
)
from services.preview.render import render_preview
from services.preview.srt import (
    expected_subtitle_cues,
    expected_subtitle_cues_wrapped,
    render_srt,
)

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C
    from services.preview.tools import PinnedTools


def _subtitle_table(
    ir: TimelineIr0C, out_dir: Path, subtitle_wrap_chars: int | None = None
) -> Path:
    items = tuple(
        item for track in ir.tracks if track.track.kind == "subtitle" for item in track.items
    )
    if subtitle_wrap_chars is not None:
        cues = expected_subtitle_cues_wrapped(items, ir.rate, subtitle_wrap_chars)
    else:
        cues = expected_subtitle_cues(items, ir.rate)
    path = out_dir / "subtitle-table.srt"
    atomic_write(path, render_srt(cues))
    return path


def _bindings(
    ir: TimelineIr0C,
    mezzanine: Path,
    media_dir: Path,
    subtitle_wrap_chars: int | None = None,
) -> PreviewMediaBindings:
    subtitle_items = tuple(
        item for track in ir.tracks if track.track.kind == "subtitle" for item in track.items
    )
    table = _subtitle_table(ir, media_dir, subtitle_wrap_chars) if subtitle_items else None
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
    timeout_seconds: float | None = None,
    presentation: PresentationRenderSettings | None = None,
    presentation_trace: TracePresentation | None = None,
) -> PreviewTraceManifest:
    """Render the review-plane preview bound to the synthesized mezzanine.

    ``presentation`` carries applied presentation overrides (subtitle
    re-wrap width, BGM gain); the bound subtitle table is wrapped with
    the same width so the render-time binding check agrees.
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    subtitle_wrap_chars = (
        presentation.subtitle_max_chars_per_line if presentation is not None else None
    )
    bindings = _bindings(ir, mezzanine, out_dir.parent / "media", subtitle_wrap_chars)
    return render_preview(
        plan, ir, bindings, out_dir, tools=tools, decision=decision,
        timeout_seconds=timeout_seconds, presentation=presentation,
        presentation_trace=presentation_trace,
    )


def render_consultation_sample(  # noqa: PLR0913 (sample adapter contract: IR/media/out + tools + presentation)
    sample_ir: TimelineIr0C,
    mezzanine: Path,
    out_dir: Path,
    *,
    tools: PinnedTools,
    timeout_seconds: float | None = None,
    presentation: PresentationRenderSettings | None = None,
    presentation_trace: TracePresentation | None = None,
) -> PreviewTraceManifest:
    """Wave-1 sample adapter (v4 contract P1-2, F6 presentation discipline).

    The CALLER verifies the base plan/version/hash and the full-IR hash
    BEFORE this call (bound together in the sample manifest); the
    existing renderer then runs with ``edit_plan=None`` and
    ``decision=None`` — the plan/IR agreement checks are neither invoked
    nor weakened, and the normal preview path is untouched.

    ``presentation``/``presentation_trace`` carry the adopted policy's
    presentation overrides through the SAME machinery as the normal
    preview (``stage_preview`` passes them identically): the bound
    subtitle table is wrapped with the same width so the render-time
    binding check agrees, and honestly-unimplemented override kinds ride
    the trace as notes — never silently unapplied, never fake effects.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    subtitle_wrap_chars = (
        presentation.subtitle_max_chars_per_line if presentation is not None else None
    )
    bindings = _bindings(sample_ir, mezzanine, out_dir.parent / "media", subtitle_wrap_chars)
    return render_preview(
        None, sample_ir, bindings, out_dir, tools=tools,
        timeout_seconds=timeout_seconds, presentation=presentation,
        presentation_trace=presentation_trace,
    )


__all__ = ["render_consultation_sample", "render_review_preview"]
