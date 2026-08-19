"""Decision-aware trace assembly: record ranges map to Decision IDs with full coverage.

Initial renders map every record range to ``initial-plan-v<N>``. After an
applied clear decision (e.g. ``p0c-remove-clear``), a regenerated preview keeps
the inherited decision for unchanged items and maps shifted/new ranges to the
applied decision id; the manifest validator rejects any gap, overlap, or
unknown decision reference.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.foundation_io import canonical_model_bytes, sha256_file

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C

from services.preview.models import (
    AppliedDecision,
    FfprobeSummary,
    PreviewFile,
    PreviewLayout,
    PreviewMediaBindings,
    PreviewTraceError,
    PreviewTraceManifest,
    RecordDecisionSpan,
    StrategyNotes,
    TimelineBinding,
    TraceDecision,
    TraceInput,
    TraceStyleTable,
)

DETERMINISM_POLICY: Final = "semantic-equivalence-h264-videotoolbox"
SUBTITLE_RUNG_SOFT: Final = "soft-mov-text-from-record-span-table"
SUBTITLE_RUNG_OMITTED: Final = "omitted-no-subtitle-items"
OVERLAY_STRATEGY: Final = "lavfi-color-corner-marker-overlay"
AUDIO_STRATEGY_BGM: Final = "item-linked-concat-plus-bgm-amix-normalize0-duration-first"
AUDIO_STRATEGY_SINGLE: Final = "item-linked-concat-single-track-no-bgm-binding"


def initial_decision_id(plan_version: str) -> str:
    return f"initial-plan-{plan_version}"


def plan_version_of(edit_plan: EditPlan0C | None) -> str:
    """The plan version a preview renders (Todo-27 default keeps ``v1``)."""

    return edit_plan.plan.plan_version if edit_plan is not None else "v1"


def coverage(
    layout: PreviewLayout, plan_version: str, decision: AppliedDecision | None
) -> tuple[RecordDecisionSpan, ...]:
    if decision is None:
        return tuple(
            RecordDecisionSpan(span=item.record_span, decision_id=initial_decision_id(plan_version))
            for item in layout.video_items
        )
    previous_items = {
        entry.item_id: entry for entry in decision.previous_trace.inputs if entry.kind == "video"
    }
    previous_tiles = decision.previous_trace.record_to_decision
    entries: list[RecordDecisionSpan] = []
    for item in layout.video_items:
        previous = previous_items.get(item.item_id)
        inherited: str | None = None
        if previous is not None and previous.record_span == item.record_span:
            inherited = next(
                (tile.decision_id for tile in previous_tiles if tile.span == item.record_span),
                None,
            )
            if inherited is None:
                raise PreviewTraceError(
                    f"previous coverage lacks a tile for unchanged item {item.item_id}"
                )
        entries.append(
            RecordDecisionSpan(
                span=item.record_span,
                decision_id=inherited if inherited is not None else decision.decision_id,
            )
        )
    return tuple(entries)


def decisions(plan_version: str, decision: AppliedDecision | None) -> tuple[TraceDecision, ...]:
    if decision is None:
        return (
            TraceDecision(
                decision_id=initial_decision_id(plan_version),
                case_id="initial-plan",
                classification="initial",
                applied=True,
                plan_version_after=plan_version,
            ),
        )
    return (
        *decision.previous_trace.decisions,
        TraceDecision(
            decision_id=decision.decision_id,
            case_id=decision.case_id,
            classification=decision.classification,
            applied=True,
            plan_version_after=decision.plan_version_after,
        ),
    )


@dataclass(frozen=True, slots=True)
class TraceContext:
    ir: TimelineIr0C
    layout: PreviewLayout
    bindings: PreviewMediaBindings
    plan_version: str
    decision: AppliedDecision | None
    styled: TraceStyleTable | None = None


def _trace_inputs(context: TraceContext) -> tuple[TraceInput, ...]:
    entries: list[TraceInput] = []
    for track in context.ir.tracks:
        for item in track.items:
            binding = context.bindings.binding_for(item.item_id)
            if binding is None:
                raise PreviewTraceError(f"trace input lacks a binding: {item.item_id}")
            entries.append(
                TraceInput(
                    item_id=item.item_id,
                    kind=item.kind,
                    media_path=binding.media_path,
                    sha256=binding.sha256,
                    source_span=item.source.span,
                    record_span=item.record_span,
                )
            )
    return tuple(entries)


def strategy_notes(context: TraceContext) -> StrategyNotes:
    return StrategyNotes(
        subtitle_rung=(
            SUBTITLE_RUNG_SOFT if context.layout.subtitle_items else SUBTITLE_RUNG_OMITTED
        ),
        overlay_strategy=OVERLAY_STRATEGY,
        audio_strategy=(
            AUDIO_STRATEGY_BGM if context.bindings.bgm is not None else AUDIO_STRATEGY_SINGLE
        ),
        determinism_policy=DETERMINISM_POLICY,
    )


def build_trace(
    context: TraceContext, preview: Path, summary: FfprobeSummary, decoded_sha: str
) -> PreviewTraceManifest:
    coverage_spans = coverage(context.layout, context.plan_version, context.decision)
    decision_entries = decisions(context.plan_version, context.decision)
    return PreviewTraceManifest(
        schema_version="preview-trace-v1",
        preview=PreviewFile(
            path=str(preview),
            sha256=sha256_file(preview),
            size=preview.stat().st_size,
            decoded_video_sha256=decoded_sha,
        ),
        timeline_binding=TimelineBinding(
            plan_version=context.plan_version,
            ir_sha256=hashlib.sha256(canonical_model_bytes(context.ir)).hexdigest(),
            total_record_frames=context.layout.total_record_frames,
            timeline_rate=context.ir.rate,
        ),
        inputs=_trace_inputs(context),
        decisions=decision_entries,
        record_to_decision=coverage_spans,
        strategy_notes=strategy_notes(context),
        ffprobe_summary=summary,
        presentation_style=context.styled,
    )


__all__ = [
    "DETERMINISM_POLICY",
    "TraceContext",
    "build_trace",
    "coverage",
    "decisions",
    "initial_decision_id",
]
