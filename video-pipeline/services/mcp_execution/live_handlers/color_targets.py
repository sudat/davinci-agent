"""Explicit-target resolution for the Task 6 DRX handler.

Product target item ids join the live ``probe_timeline_structure``
readback by their committed record span (absolute frames = plan span +
timeline start) into the explicit ``(track_index, item_index)``
coordinates the vendor item actions require — never a selection, never
the vendor's implicit V1/item0 default. Zero matches are typed refusals;
when several placed items share one record span (measured on
v44-real-01: every native subtitle cue card overlays the clip beneath it
at the identical span), the target's committed SOURCE span is the
deterministic independent identity that must confirm exactly one item —
duplicates without a confirmed identity refuse typed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from services.mcp_client.ops_models import (
    GradeVersionSnapshotResult,
    McpActionOutcome,
    NodeGraphResult,
    StructureItem,
    StructureSnapshot,
)
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveSessionContext,
    require_ok,
)
from services.mcp_execution.live_handlers.placement import (
    SOURCE_READBACK_TOLERANCE_FRAMES,
)

if TYPE_CHECKING:
    from services.contracts.primitives import SourceFrameSpan
    from services.mcp_execution.plan_payloads import ColorTargetPayload

#: The structure readback's source bounds share the measured
#: GetSourceStartFrame ±1 drift with source_range_report (vendor truth in
#: common.py); the identity join accepts exactly that tolerance and no more.
_SOURCE_IDENTITY_TOLERANCE_FRAMES: Final = SOURCE_READBACK_TOLERANCE_FRAMES


@dataclass(frozen=True)
class ResolvedTarget:
    """One product target resolved to explicit vendor coordinates."""

    payload: ColorTargetPayload
    timeline_item_id: str
    track_index: int
    item_index: int


#: Measured deadline for ONE video-only probe_timeline_structure walk
#: (2026-08-29, fresh gated session, read-only: 1135.5 s on the fully-built
#: representative timeline — the same growth that pushed the per-track scans
#: from 35 s to ~170-336 s in two days; the walk ALSO exceeds 1500 s when
#: resumed inside a finishing session after placements + the subtitle track-2
#: scan, and completed fresh only at ~1135 s). The ceiling moves
#: 1500 → 3600 s; re-measure before raising again. ONLY this vendor
#: action carries the budget.
COLOR_STRUCTURE_PROBE_TIMEOUT_SECONDS: Final = 3600.0


def structure_snapshot(ctx: LiveSessionContext) -> StructureSnapshot:
    snap = StructureSnapshot.model_validate(
        ctx.transport(
            "timeline",
            "probe_timeline_structure",
            {"track_types": ["video"], "include_markers": False},
            timeout_seconds=COLOR_STRUCTURE_PROBE_TIMEOUT_SECONDS,
        )
    )
    require_ok(snap, "structure-readback")
    return snap


def video_items(snap: StructureSnapshot) -> list[StructureItem]:
    group = snap.tracks.get("video")
    return [item for row in group.tracks for item in row.items] if group else []


def _identity_filtered(
    target: ColorTargetPayload,
    span: SourceFrameSpan,
    hits: list[StructureItem],
) -> list[StructureItem]:
    """Narrow record-span hits by the committed SOURCE identity.

    Only items whose structure readback carries the exact committed source
    frames survive; zero survivors is the typed ``target-identity-unmatched``
    refusal naming the identity join that failed — never a positional pick
    among duplicates, never an unconfirmed single hit.
    """

    matches = [
        item
        for item in hits
        if item.source_start is not None
        and item.source_end is not None
        and abs(item.source_start - span.start_frame) <= _SOURCE_IDENTITY_TOLERANCE_FRAMES
        and abs(item.source_end - span.end_frame) <= _SOURCE_IDENTITY_TOLERANCE_FRAMES
    ]
    if matches:
        return matches
    if all(item.source_start is None and item.source_end is None for item in hits):
        raise LiveAdapterError(
            "target-identity-unmatched",
            f"target {target.item_id} record span matches {len(hits)} items but the "
            "structure readback carries no source frame identity "
            f"(source_start/source_end); committed source frames "
            f"[{span.start_frame}, {span.end_frame}) cannot be joined",
        )
    observed = ", ".join(f"[{item.source_start},{item.source_end})" for item in hits)
    raise LiveAdapterError(
        "target-identity-unmatched",
        f"target {target.item_id} committed source frames "
        f"[{span.start_frame}, {span.end_frame}) match no record-span hit "
        f"(items carry {observed})",
    )


def resolve_targets(
    ctx: LiveSessionContext, snap: StructureSnapshot, wanted: tuple[ColorTargetPayload, ...]
) -> list[ResolvedTarget]:
    if ctx.timeline_start is None:
        raise LiveAdapterError("timeline-not-prepared", "prepare first")
    resolved: list[ResolvedTarget] = []
    for target in wanted:
        start = ctx.timeline_start + target.record_span.start_frame
        end = ctx.timeline_start + target.record_span.end_frame
        hits = [item for item in video_items(snap) if item.start == start and item.end == end]
        if not hits:
            raise LiveAdapterError(
                "target-not-placed",
                f"target {target.item_id} record span "
                f"[{target.record_span.start_frame}, {target.record_span.end_frame}) "
                "matches no video item in the structure readback",
            )
        if target.source_span is None:
            candidates = hits
        else:
            candidates = _identity_filtered(target, target.source_span, hits)
        hit = candidates[0]
        if len(candidates) > 1 or hit.timeline_item_id is None or hit.track_index is None or hit.item_index is None:  # noqa: E501
            raise LiveAdapterError(
                "target-ambiguous",
                f"target {target.item_id} record span matches {len(candidates)} items; "
                "explicit coordinates require exactly one",
            )
        resolved.append(
            ResolvedTarget(
                payload=target,
                timeline_item_id=str(hit.timeline_item_id),
                track_index=int(hit.track_index),
                item_index=int(hit.item_index),
            )
        )
    return resolved


def coords(target: ResolvedTarget) -> dict[str, object]:
    return {
        "track_type": "video",
        "track_index": target.track_index,
        "item_index": target.item_index,
    }


def representative_frame(target: ResolvedTarget) -> int:
    span = target.payload.record_span
    return span.start_frame + (span.end_frame - span.start_frame) // 2


def grade_versions(ctx: LiveSessionContext, target: ResolvedTarget) -> GradeVersionSnapshotResult:
    snap = GradeVersionSnapshotResult.model_validate(
        ctx.transport("timeline_item_color", "grade_version_snapshot", coords(target))
    )
    require_ok(snap, "grade-version-readback")
    return snap


def node_graph(ctx: LiveSessionContext, target: ResolvedTarget) -> NodeGraphResult:
    graph = NodeGraphResult.model_validate(
        ctx.transport("timeline_item_color", "probe_node_graph", coords(target))
    )
    require_ok(graph, "node-graph-readback")
    return graph


def graph_signature(graph: NodeGraphResult) -> tuple[object, ...]:
    return (graph.num_nodes, tuple((n.node_index, str(n.label), str(n.lut)) for n in graph.nodes))


def add_version(ctx: LiveSessionContext, target: ResolvedTarget, name: str) -> None:
    require_ok(
        McpActionOutcome.model_validate(
            ctx.transport(
                "timeline_item_color", "add_version", {**coords(target), "name": name, "type": 0}
            )
        ),
        "add-version",
    )


#: MEASURED 2026-08-29 (T9, this pinned Resolve 21.0.4.5 + MCP build): the
#: vendor ``safe_apply_drx`` rotates the target's video layer 90 degrees
#: clockwise in the composite while Inspector ``RotationAngle`` reads 0.0,
#: and Inspector +90.0 (sign inverted vs the visual rotation in this build)
#: restores upright — bound-tested twice, then confirmed on the full fresh
#: native render. The compensation rides the SAME grade step through the
#: guarded ``timeline_item.set_transform`` (preflight/readback/idempotent);
#: the fresh-render orientation QC fail-closes if a build behaves differently.
DRX_ORIENTATION_COMPENSATION_ANGLE: Final = 90.0


def compensate_orientation(
    ctx: LiveSessionContext, target: ResolvedTarget
) -> dict[str, object]:
    """Undo the vendor DRX layer rotation for one target (idempotent)."""

    from services.mcp_execution.live_handlers.transform import (  # noqa: PLC0415
        apply_transform,
    )

    result = apply_transform(
        ctx,
        "set_transform",
        {
            "action": "set_transform",
            "track_index": target.track_index,
            "item_index": target.item_index,
            "rotation_angle": DRX_ORIENTATION_COMPENSATION_ANGLE,
            "target_item_id": str(target.payload.item_id),
        },
    )
    return {
        "rotation_angle": DRX_ORIENTATION_COMPENSATION_ANGLE,
        "applied": bool(result.get("applied")),
    }


__all__ = [
    "DRX_ORIENTATION_COMPENSATION_ANGLE",
    "ResolvedTarget",
    "add_version",
    "compensate_orientation",
    "coords",
    "grade_versions",
    "graph_signature",
    "node_graph",
    "representative_frame",
    "resolve_targets",
    "structure_snapshot",
    "video_items",
]
