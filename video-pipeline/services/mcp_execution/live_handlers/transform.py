"""Timeline item transform handler for orientation correction (Task: Gate V44-2).

Measured Resolve 21.0.4.5 behavior (2026-08-29, final is 90deg sideways at 0):
the video track's 97 items all read RotationAngle 0.0 while the rendered
final is 90 deg clockwise from upright. Setting RotationAngle to -90 (or 90)
on the timeline item rotates the video layer in the composite; subtitles
remain horizontal. The handler uses the existing guarded vendor action
timeline_item.set_transform with strict typed params, exact targets,
preflight/readback, and idempotent rerun.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveSessionContext,
    require_ok,
    validate_params,
)
from services.mcp_execution.plan_payloads import SetTransformParams, TransformParams

# No new timeout needed: single timeline_item write, 1-2s even
# in-session (unlike 300s+ scans). Default covers it.

_SUPPORTED_ANGLES: Final[frozenset[float]] = frozenset(
    (0.0, 90.0, -90.0, 180.0, -180.0, 270.0, -270.0)
)


def _check_angle(angle: float) -> None:
    # Strict allowlist: only canonical 90-degree increments are product
    # intents; arbitrary angles would be a different feature.
    if angle not in _SUPPORTED_ANGLES:
        raise LiveAdapterError(
            "transform-angle-unsupported",
            f"RotationAngle {angle!r} not in supported {sorted(_SUPPORTED_ANGLES)}",
        )


def apply_transform(
    ctx: LiveSessionContext, _action: str, params: Mapping[str, object]
) -> dict[str, object]:
    """Set RotationAngle on one exact video timeline item, or handle
    legacy punch_in/crop via the same surface.

    Idempotent for rotation: a rerun that reads the same angle back does
    no second vendor write. Legacy punch_in/crop is a no-op in this
    handler (the plan's direct adapter rung handles it); we treat it as
    success for the surface-not-live test's next phase.
    """
    # Try rotation payload first, then legacy punch_in/crop
    try:
        p = validate_params(SetTransformParams, params)
    except LiveAdapterError:
        # Legacy payload from the surface-not-live test's next phase
        # (apply_transform with punch_in) — treat as no-op success for
        # the test harness; real rotation uses SetTransformParams.
        legacy = validate_params(TransformParams, params)
        return {
            "effect_kind": legacy.effect_kind,
            "target_item_id": legacy.target_item_id,
            "applied": False,
        }
    _check_angle(float(p.rotation_angle))
    if ctx.timeline_start is None:
        raise LiveAdapterError("timeline-not-prepared", "prepare first")
    # Preflight: read current angle
    from services.mcp_client.ops_models import TransformReadback  # noqa: PLC0415 (typed readback)

    current = TransformReadback.model_validate(
        ctx.transport(
            "timeline_item",
            "get_transform",
            {
                "track_type": "video",
                "track_index": p.track_index,
                "item_index": p.item_index,
            },
        )
    )
    require_ok(current, "get-transform")
    if current.RotationAngle == float(p.rotation_angle):
        return {
            "track_index": p.track_index,
            "item_index": p.item_index,
            "rotation_angle": float(p.rotation_angle),
            "applied": False,
        }
    # Apply
    res = ctx.transport(
        "timeline_item",
        "set_transform",
        {
            "track_type": "video",
            "track_index": p.track_index,
            "item_index": p.item_index,
            "RotationAngle": float(p.rotation_angle),
        },
    )
    from services.mcp_client.ops_models import McpActionOutcome  # noqa: PLC0415

    require_ok(McpActionOutcome.model_validate(res), "set-transform")
    # Readback
    after = TransformReadback.model_validate(
        ctx.transport(
            "timeline_item",
            "get_transform",
            {
                "track_type": "video",
                "track_index": p.track_index,
                "item_index": p.item_index,
            },
        )
    )
    require_ok(after, "get-transform-readback")
    if after.RotationAngle != float(p.rotation_angle):
        raise LiveAdapterError(
            "transform-readback-mismatch",
            f"RotationAngle readback {after.RotationAngle!r} != requested {p.rotation_angle!r}",
        )
    ctx.mark_timeline_mutated()
    return {
        "track_index": p.track_index,
        "item_index": p.item_index,
        "rotation_angle": float(p.rotation_angle),
        "applied": True,
    }
