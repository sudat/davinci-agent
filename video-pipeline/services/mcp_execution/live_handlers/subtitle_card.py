"""Nested-timeline card construction for native subtitle cues (Task 4).

Mechanics only, parameterized by the caller's style binding: create one
card timeline per cue, put a Fusion Title on it, write the exact text plus
presentation inputs, place the card's MediaPoolItem on the overlay track at
the exact record span, and verify everything through independent readbacks.
Style values, comparison, and drift failures live in ``subtitle_style``.
Route rationale and probe evidence: see ``live_handlers/subtitle.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from services.mcp_client.ops_models import (
    AppendResult,
    FusionInputsResult,
    FusionInputValueReadback,
    McpActionOutcome,
    MediaPoolItemResult,
    TextPlusReadback,
    TimelineResult,
    TrackCountResult,
    TrackItemsResult,
)
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveSessionContext,
    require_ok,
)
from services.mcp_execution.live_handlers.subtitle_style import (
    SHADOW_ENABLE_INPUT,
    CardReadback,
    StyleBinding,
    verify_card_style,
)

if TYPE_CHECKING:
    from services.mcp_execution.plan_payloads import SubtitleCuePayload

#: The overlay track native cues land on (first track above primary).
OVERLAY_TRACK_INDEX: Final = 2

#: Measured deadline for ONE overlay-track scan (verifier-probed 2026-08-27:
#: get_items_in_track(video, track 2) took 67.787 s for the 100 cue-card
#: items on the representative timeline — the 30 s transport default timed
#: out and poisoned the sequential server session for later operations).
#: RE-MEASURED 2026-08-29 (fresh gated session, read-only): the same scan
#: took 336.5 s — the same server-state growth the other walk costs show —
#: and **>600 s when resumed inside a finishing session** after placements
#: (the server's per-call cost grows with session call count). The ceiling
#: moved 600 → 1200 s (~3.5x over the fresh 336.5 s, >600 s in-session).
#: ONLY this vendor action carries the budget; everything else keeps the
#: transport default.
SUBTITLE_TRACK_SCAN_TIMEOUT_SECONDS: Final = 7200.0

#: Measured deadline for ONE timeline switch in the subtitle flow
#: (verifier-probed 2026-08-27: switching to
#: a cue-card timeline took 17.110s near cue 25, 32.775s near cue 50,
#: 51.244s near cue 75, and 65.898s for the final sampled card — growing
#: ~0.66s per cue position, while card text/style reads stayed 1.6-2.9s
#: and returning to main stayed 1.3-2.0s. The 30s transport default killed
#: the rerun once switches crossed it (~cue 50). ~2.7x headroom over the
#: final measured switch covers ~270 cues; RE-MEASURED 2026-08-29
#: in-session after placements, switch exceeded 180 s — ceiling 180 → 600 s.
#: ONLY
#: the subtitle switch seam (restore, card-in, main-out) carries it.
SUBTITLE_CARD_SWITCH_TIMEOUT_SECONDS: Final = 600.0

#: The Text+ tool a Fusion Title exposes (measured; issue #74 ledger entry).
TEMPLATE_TOOL: Final = "Template"

#: The Fusion Title template name measured to insert and render on 21.0.4.5.
TITLE_TEMPLATE: Final = "Text+"


def card_scope() -> dict[str, object]:
    return {
        "timeline_item": {"track_type": "video", "track_index": 1, "item_index": 0}
    }


def is_cue_card_name(name: object, timeline_name: str) -> bool:
    """True when ``name`` is exactly a card-timeline name this handler
    mints: ``{timeline_name}-cue-{cue_id}`` with a non-empty cue id (the
    media-pool identity readback carries for a native cue card).

    Must stay in lockstep with the ``card_name`` construction in
    ``create_cue`` and the rerun scan in ``apply_subtitles`` — a drift
    here would mis-identify (or miss) placed cards downstream.
    """
    if not isinstance(name, str):
        return False
    prefix = f"{timeline_name}-cue-"
    return name.startswith(prefix) and len(name) > len(prefix)


def items_in_track(ctx: LiveSessionContext, track_index: int) -> TrackItemsResult:
    result = TrackItemsResult.model_validate(
        ctx.transport(
            "timeline",
            "get_items_in_track",
            {"track_type": "video", "track_index": track_index},
            timeout_seconds=SUBTITLE_TRACK_SCAN_TIMEOUT_SECONDS,
        )
    )
    require_ok(result, "get-items-in-track")
    return result


def ensure_overlay_track(ctx: LiveSessionContext) -> None:
    count = TrackCountResult.model_validate(
        ctx.transport("timeline", "get_track_count", {"track_type": "video"})
    )
    require_ok(count, "get-track-count")
    if (count.count or 0) < OVERLAY_TRACK_INDEX:
        require_ok(
            McpActionOutcome.model_validate(
                ctx.transport("timeline", "add_track", {"track_type": "video"})
            ),
            "add-overlay-track",
        )


def set_current(ctx: LiveSessionContext, name: str, label: str) -> None:
    require_ok(
        McpActionOutcome.model_validate(
            ctx.transport(
                "timeline",
                "set_current",
                {"name": name},
                timeout_seconds=SUBTITLE_CARD_SWITCH_TIMEOUT_SECONDS,
            )
        ),
        label,
    )


def cue_text_on_card(ctx: LiveSessionContext) -> str:
    text = TextPlusReadback.model_validate(
        ctx.transport(
            "fusion_comp",
            "get_text_plus",
            {**card_scope(), "tool_name": TEMPLATE_TOOL},
        )
    )
    require_ok(text, "get-text-plus")
    return text.text or ""


def fusion_input(ctx: LiveSessionContext, input_name: str) -> FusionInputValueReadback:
    value = FusionInputValueReadback.model_validate(
        ctx.transport(
            "fusion_comp",
            "get_input",
            {**card_scope(), "tool_name": TEMPLATE_TOOL, "input_name": input_name},
        )
    )
    require_ok(value, "get-input")
    return value


def _prepared_main_name(ctx: LiveSessionContext) -> str:
    if ctx.current_timeline_name is None or ctx.timeline_start is None:
        raise LiveAdapterError("timeline-not-prepared", "prepare first")
    return ctx.current_timeline_name


def read_card(ctx: LiveSessionContext, card_name: str) -> CardReadback:
    """Independent text+style readback through the card timeline."""
    main_name = _prepared_main_name(ctx)
    set_current(ctx, card_name, "set-current-card")
    try:
        return CardReadback(
            text=cue_text_on_card(ctx),
            font=fusion_input(ctx, "Font").value,
            size=fusion_input(ctx, "Size").value,
            center=fusion_input(ctx, "Center").value,
            shadow_enabled=fusion_input(ctx, SHADOW_ENABLE_INPUT).value,
        )
    finally:
        set_current(ctx, main_name, "set-current-main")


def create_cue(
    ctx: LiveSessionContext, cue: SubtitleCuePayload, style: StyleBinding
) -> dict[str, object]:
    """Create one native cue at the exact half-open span; verify readbacks.

    The shared context must already carry the prepared session (current
    timeline name and timeline start). Returns the readback-sourced style
    evidence (font, size, and center as read back from the placed card's
    Template tool).
    """
    main_name = _prepared_main_name(ctx)
    timeline_start = int(ctx.timeline_start or 0)
    card_name = f"{main_name}-cue-{cue.cue_id}"
    text = cue.text
    record_start = cue.record_span.start_frame
    record_end = cue.record_span.end_frame
    card = TimelineResult.model_validate(
        ctx.transport("media_pool", "create_timeline", {"name": card_name})
    )
    require_ok(card, "create-card-timeline")
    set_current(ctx, card_name, "set-current-card")
    try:
        require_ok(
            McpActionOutcome.model_validate(
                ctx.transport("timeline", "insert_fusion_title", {"name": TITLE_TEMPLATE})
            ),
            "insert-fusion-title",
        )
        inputs = FusionInputsResult.model_validate(
            ctx.transport(
                "fusion_comp",
                "safe_set_inputs",
                {
                    **card_scope(),
                    "tool_name": TEMPLATE_TOOL,
                    "inputs": {
                        "StyledText": text,
                        "Font": style.font,
                        **dict(style.inputs),
                    },
                    "readback": True,
                },
            )
        )
        require_ok(inputs, "set-cue-inputs")
        styled = inputs.results.get("StyledText")
        styled_value = styled.value if styled is not None else None
        if styled is None or not styled.success or styled.value != text:
            raise LiveAdapterError(
                "subtitle-text-mismatch",
                f"StyledText readback {styled_value!r} != committed {text!r}",
            )
        placed_style = CardReadback(
            text=text,
            font=fusion_input(ctx, "Font").value,
            size=fusion_input(ctx, "Size").value,
            center=fusion_input(ctx, "Center").value,
            shadow_enabled=fusion_input(ctx, SHADOW_ENABLE_INPUT).value,
        )
        verify_card_style(f"card {card_name}", placed_style, style)
        mpi = MediaPoolItemResult.model_validate(
            ctx.transport("timeline", "get_media_pool_item", {})
        )
        require_ok(mpi, "get-card-media-pool-item")
        if not mpi.id:
            raise LiveAdapterError("card-media-pool-item-missing", "no card item id")
    finally:
        set_current(ctx, main_name, "set-current-main")
    abs_start = timeline_start + record_start
    span_len = record_end - record_start
    require_ok(
        AppendResult.model_validate(
            ctx.transport(
                "media_pool",
                "append_to_timeline",
                {
                    "clip_infos": [
                        {
                            "clip_id": mpi.id,
                            "media_type": 1,
                            "start_frame": 0,
                            "end_frame": span_len,
                            "record_frame": abs_start,
                            "record_frame_mode": "absolute",
                            "track_index": OVERLAY_TRACK_INDEX,
                        }
                    ]
                },
            )
        ),
        "append-cue",
    )
    ctx.mark_timeline_mutated()
    if not any(
        row.start == abs_start and row.end == abs_start + span_len
        for row in items_in_track(ctx, OVERLAY_TRACK_INDEX).items
    ):
        raise LiveAdapterError(
            "subtitle-span-mismatch",
            f"no overlay item at record [{abs_start},{abs_start + span_len})",
        )
    card = read_card(ctx, card_name)
    if card.text != text:
        raise LiveAdapterError(
            "subtitle-text-mismatch", f"card {card_name} text drifted after placement"
        )
    return verify_card_style(f"card {card_name}", card, style)


__all__ = [
    "OVERLAY_TRACK_INDEX",
    "SUBTITLE_CARD_SWITCH_TIMEOUT_SECONDS",
    "SUBTITLE_TRACK_SCAN_TIMEOUT_SECONDS",
    "TEMPLATE_TOOL",
    "create_cue",
    "cue_text_on_card",
    "ensure_overlay_track",
    "fusion_input",
    "is_cue_card_name",
    "items_in_track",
    "read_card",
    "set_current",
]
