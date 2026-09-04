"""Nested-card mechanics for telop on V3 (mirror of ``subtitle_card``).

Same proven route as the subtitle cue cards (DESIGN telop-nested §1.3,
§7): one mini timeline per card holds ONE band-including Fusion Title
(template asset below); the card's MediaPoolItem lands on the telop track
(V3) through ``append_to_timeline`` clipInfos with absolute record frames
and half-open spans. Tile math lives in ``telop_spans`` (pure); scan and
switch deadlines reuse the subtitle-measured ceilings (DESIGN §8).

Template install (WBS-3 / live-gate scope, NOT this module): copy the
tracked asset to ``Fusion/Templates/Edit/Titles/`` under the Resolve
user directory and restart Resolve — the Effects Library matches the
template by exact FILENAME and only registers it after a restart
(WBS-1 probe check-b).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.mcp_client.ops_models import (
    AppendResult,
    FusionInputsResult,
    McpActionOutcome,
    MediaPoolItemResult,
    TimelineResult,
    TrackCountResult,
)
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveSessionContext,
    require_ok,
)
from services.mcp_execution.live_handlers.subtitle_card import (
    TEMPLATE_TOOL,
    card_scope,
    cue_text_on_card,
    fusion_input,
    set_current,
)
from services.mcp_execution.live_handlers.telop_spans import contiguous_runs
from services.mcp_execution.live_handlers.telop_style import (
    TelopCardBinding,
    TelopCardReadback,
    verify_telop_card,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The telop track (second track above primary; subtitle cues own V2).
TELOP_TRACK_INDEX: Final = 3

#: The inner Text+ tool inside the tracked band template's Template
#: group. The template ships this tool's background RGBA baked to
#: opaque white and exposes NO published Template input for it — the
#: group route cannot reach it (write-path bisect 2026-09-04,
#: ``private/runtime/sol-writepath-probe-20260904/evidence.json``:
#: every group-only rung W0..W7 renders a fully white frame).
TEXT_TOOL: Final = "Text"

#: The white-removal prefix written DIRECTLY on the inner Text tool
#: before the published group inputs — the only route proven to clear
#: the baked background (probe rung P3: this prefix + the full
#: 19-input group set with ``CharacterSpacing=1.0`` renders master
#: parity; safe in all rungs B1+).
_TEXT_BACKGROUND_CLEAR: Final[dict[str, int]] = {
    "Red": 0,
    "Green": 0,
    "Blue": 0,
    "Alpha": 0,
}

#: The tracked product template asset. The Effects Library name equals
#: the exact FILENAME (WBS-1 probe check-b, Japanese UI); bytes are the
#: probe-proven canonical copy (sha256 below — asset integrity lock).
#: Revision 2 (sol-c gate round-1 white-render fix): MainOutput rewired
#: from the mis-wired KeyStretcher to the band Merge (TextOverBand) and
#: the leftover KeyStretcher node removed. Superseded revision 1
#: (WBS-1 probe provenance): b1c6683c2422681120dcdaa91f2f610fa6af9401ab81f5d85a638c3669e3c1ed
TELOP_TEMPLATE: Final = "FVP Telop Band v1"
TELOP_TEMPLATE_SHA256: Final = "a18b90f83adcf8d1de554bfc07fb81cbd03484504a8e329d7b5cbd57f09b937a"
TELOP_TEMPLATE_ASSET: Final[Path] = (
    Path(__file__).resolve().parents[3]
    / "config"
    / "production-kit"
    / "titles"
    / f"{TELOP_TEMPLATE}.setting"
)


def telop_card_name(timeline_name: str, card_id: str) -> str:
    return f"{timeline_name}-telop-{card_id}"


def ensure_telop_track(ctx: LiveSessionContext) -> None:
    """Ensure the telop track exists (V3; the V2 ensure's multi-add form)."""
    count = TrackCountResult.model_validate(
        ctx.transport("timeline", "get_track_count", {"track_type": "video"})
    )
    require_ok(count, "get-track-count")
    for _ in range(max(0, TELOP_TRACK_INDEX - (count.count or 0))):
        require_ok(
            McpActionOutcome.model_validate(
                ctx.transport("timeline", "add_track", {"track_type": "video"})
            ),
            "add-telop-track",
        )


def append_telop_spans(
    ctx: LiveSessionContext, clip_id: str, spans: Sequence[tuple[int, int]]
) -> int:
    """Place absolute half-open spans of one card: one batched append per
    contiguous run (WBS-1 check-d). Returns the placed clip count."""
    placed = 0
    for run in contiguous_runs(spans):
        require_ok(
            AppendResult.model_validate(
                ctx.transport(
                    "media_pool",
                    "append_to_timeline",
                    {
                        "clip_infos": [
                            {
                                "clip_id": clip_id,
                                "media_type": 1,
                                "start_frame": 0,
                                "end_frame": end - start,
                                "record_frame": start,
                                "record_frame_mode": "absolute",
                                "track_index": TELOP_TRACK_INDEX,
                            }
                            for start, end in run
                        ]
                    },
                )
            ),
            "append-telop",
        )
        placed += len(run)
    return placed


def create_telop_card(ctx: LiveSessionContext, card_name: str, binding: TelopCardBinding) -> str:
    """Create one card timeline holding the band title with the bound
    inputs — the white-removal direct prefix on the inner Text tool
    first, then the published group inputs; returns the card's media
    pool item id (subtitle ``create_cue`` mechanics, template name and
    input set swapped)."""
    main_name = ctx.current_timeline_name
    if main_name is None:
        raise LiveAdapterError("timeline-not-prepared", "prepare first")
    card = TimelineResult.model_validate(
        ctx.transport("media_pool", "create_timeline", {"name": card_name})
    )
    require_ok(card, "create-card-timeline")
    set_current(ctx, card_name, "set-current-card")
    try:
        require_ok(
            McpActionOutcome.model_validate(
                ctx.transport("timeline", "insert_fusion_title", {"name": TELOP_TEMPLATE})
            ),
            "insert-fusion-title",
        )
        cleared = FusionInputsResult.model_validate(
            ctx.transport(
                "fusion_comp",
                "safe_set_inputs",
                {
                    **card_scope(),
                    "tool_name": TEXT_TOOL,
                    "inputs": dict(_TEXT_BACKGROUND_CLEAR),
                    "readback": True,
                },
            )
        )
        require_ok(cleared, "clear-text-background")
        for axis, value in _TEXT_BACKGROUND_CLEAR.items():
            row = cleared.results.get(axis)
            if row is None or not row.success or row.value != value:
                raise LiveAdapterError(
                    "telop-text-background-clear-failed",
                    f"{axis} readback {row!r} != {value}",
                )
        inputs = FusionInputsResult.model_validate(
            ctx.transport(
                "fusion_comp",
                "safe_set_inputs",
                {
                    **card_scope(),
                    "tool_name": TEMPLATE_TOOL,
                    "inputs": dict(binding.inputs),
                    "readback": True,
                },
            )
        )
        require_ok(inputs, "set-telop-inputs")
        styled = inputs.results.get("StyledText")
        if styled is None or not styled.success or styled.value != binding.styled_text:
            raise LiveAdapterError(
                "telop-text-mismatch",
                f"StyledText readback {styled.value if styled else None!r}"
                f" != committed {binding.styled_text!r}",
            )
        verify_telop_card(f"card {card_name}", _bound_readback(ctx), binding)
        mpi = MediaPoolItemResult.model_validate(
            ctx.transport("timeline", "get_media_pool_item", {})
        )
        require_ok(mpi, "get-card-media-pool-item")
        if not mpi.id:
            raise LiveAdapterError("card-media-pool-item-missing", "no card item id")
    finally:
        set_current(ctx, main_name, "set-current-main")
    return mpi.id


def existing_card_media_pool_item(ctx: LiveSessionContext, card_name: str) -> str:
    """Open an already-created card to recover its media pool item id
    (partial-run top-up path; never re-creates the card)."""
    main_name = ctx.current_timeline_name
    if main_name is None:
        raise LiveAdapterError("timeline-not-prepared", "prepare first")
    set_current(ctx, card_name, "set-current-card")
    try:
        mpi = MediaPoolItemResult.model_validate(
            ctx.transport("timeline", "get_media_pool_item", {})
        )
        require_ok(mpi, "get-card-media-pool-item")
        if not mpi.id:
            raise LiveAdapterError("card-media-pool-item-missing", "no card item id")
    finally:
        set_current(ctx, main_name, "set-current-main")
    return mpi.id


def _bound_readback(ctx: LiveSessionContext) -> TelopCardReadback:
    return TelopCardReadback(
        text=cue_text_on_card(ctx),
        font=fusion_input(ctx, "Font").value,
        size=fusion_input(ctx, "Size").value,
        text_pos=fusion_input(ctx, "TextPos").value,
    )


def read_telop_card(ctx: LiveSessionContext, card_name: str) -> TelopCardReadback:
    """Independent text+style readback through the card timeline."""
    main_name = ctx.current_timeline_name
    if main_name is None:
        raise LiveAdapterError("timeline-not-prepared", "prepare first")
    set_current(ctx, card_name, "set-current-card")
    try:
        return _bound_readback(ctx)
    finally:
        set_current(ctx, main_name, "set-current-main")


__all__ = [
    "TELOP_TEMPLATE",
    "TELOP_TEMPLATE_ASSET",
    "TELOP_TEMPLATE_SHA256",
    "TELOP_TRACK_INDEX",
    "TEXT_TOOL",
    "append_telop_spans",
    "create_telop_card",
    "ensure_telop_track",
    "existing_card_media_pool_item",
    "read_telop_card",
    "telop_card_name",
]
