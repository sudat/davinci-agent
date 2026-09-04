"""Native telop handler: nested cards on V3 (DESIGN telop-nested WBS-2).

The ``apply_subtitles`` mirror for the three telop kinds: resolve each
card's binding through the channel profile ``telop_style``, expand the
persistent span into chapter-gapped tiles at the measured card length,
create (or re-open) one card timeline per card, place its spans on V3
with one batched append per contiguous tile run, and verify everything
through ONE fresh track scan plus through-nesting card readbacks. Reruns
detect placed spans exactly and never create duplicates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from services.creative_plan.presentation_intents import load_default_profile
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveAdapterUnsupportedError,
    LiveSessionContext,
    validate_params,
)
from services.mcp_execution.live_handlers.subtitle_card import (
    items_in_track,
    set_current,
)
from services.mcp_execution.live_handlers.telop_card import (
    TELOP_TRACK_INDEX,
    append_telop_spans,
    create_telop_card,
    ensure_telop_track,
    existing_card_media_pool_item,
    read_telop_card,
    telop_card_name,
)
from services.mcp_execution.live_handlers.telop_spans import (
    TELOP_TILE_FRAMES,
    tile_spans,
    verify_track_spans,
)
from services.mcp_execution.live_handlers.telop_style import (
    TelopCardBinding,
    resolve_telop_binding,
    verify_telop_card,
)
from services.mcp_execution.plan_payloads import TelopCardPayload, TelopParams

if TYPE_CHECKING:
    from services.creative_plan.presentation_intents import TelopStyle


def _resolve_telop_style(style_profile_id: str) -> TelopStyle:
    """Bind a style reference through the channel presentation profile.

    The accepted id is the profile's own ``telop_style.recipe_id`` — any
    other reference is a typed refusal, never a silent default."""
    style = load_default_profile().telop_style
    if style_profile_id != style.recipe_id:
        raise LiveAdapterUnsupportedError(
            "telop-style-profile-unsupported",
            f"no telop mapping for {style_profile_id!r} (profile carries"
            f" {style.recipe_id!r})",
        )
    return style


def _placement_spans(
    card: TelopCardPayload, gaps: tuple[tuple[int, int], ...]
) -> tuple[tuple[int, int], ...]:
    """The card's half-open placement spans (relative record frames):
    tiles for the persistent kind, one span for opening/chapter."""
    start = card.record_span.start_frame
    end = card.record_span.end_frame
    if card.kind == "persistent":
        return tile_spans((start, end), gaps, TELOP_TILE_FRAMES)
    if end - start > TELOP_TILE_FRAMES:
        raise LiveAdapterError(
            "telop-span-exceeds-card-length",
            f"card {card.card_id} ({card.kind}) span [{start},{end}) exceeds"
            f" the {TELOP_TILE_FRAMES}-frame card media; split into cards",
        )
    return ((start, end),)


def _place_card(
    ctx: LiveSessionContext,
    binding: TelopCardBinding,
    card_name: str,
    abs_spans: tuple[tuple[int, int], ...],
    present: set[tuple[int, int]],
) -> bool:
    """Create-or-reopen the card and append its missing spans. Returns
    whether any placement mutation happened."""
    missing = [span for span in abs_spans if span not in present]
    if not missing:
        return False
    if any(span in present for span in abs_spans):
        clip_id = existing_card_media_pool_item(ctx, card_name)
    else:
        clip_id = create_telop_card(ctx, card_name, binding)
    append_telop_spans(ctx, clip_id, missing)
    ctx.mark_timeline_mutated()
    return True


def apply_telop(
    ctx: LiveSessionContext, _action: str, params: Mapping[str, object]
) -> dict[str, object]:
    """Place every committed telop card natively; reruns place no duplicates."""
    p = validate_params(TelopParams, params)
    style = _resolve_telop_style(str(p.style_profile_id))
    if ctx.timeline_start is None or ctx.current_timeline_name is None:
        raise LiveAdapterError("timeline-not-prepared", "prepare first")
    main_name = ctx.current_timeline_name
    timeline_start = int(ctx.timeline_start)
    gaps = tuple(
        (card.record_span.start_frame, card.record_span.end_frame)
        for card in p.cards
        if card.kind == "chapter"
    )
    # Resolve every binding and placement span BEFORE any vendor call: an
    # unfittable text or an over-long single card refuses with Resolve
    # untouched (subtitle refuses malformed params at the same boundary).
    resolved = [
        (
            card,
            resolve_telop_binding(card.kind, card.text, style),
            _placement_spans(card, gaps),
        )
        for card in p.cards
    ]
    # Retry-state safety (subtitle precedent: a timed-out attempt can leave
    # a card CURRENT): re-select the prepared main timeline FIRST.
    set_current(ctx, main_name, "set-current-main-restore")
    ensure_telop_track(ctx)
    snapshot = items_in_track(ctx, TELOP_TRACK_INDEX).items
    present = {
        (row.start, row.end)
        for row in snapshot
        if row.start is not None and row.end is not None
    }
    rows: list[dict[str, object]] = []
    expected: list[tuple[int, int]] = []
    appended = False
    for card, binding, spans in resolved:
        abs_spans = tuple(
            (timeline_start + start, timeline_start + end) for start, end in spans
        )
        expected.extend(abs_spans)
        card_name = telop_card_name(main_name, str(card.card_id))
        if _place_card(ctx, binding, card_name, abs_spans, present):
            appended = True
        readback = read_telop_card(ctx, card_name)
        if readback.text != binding.styled_text:
            raise LiveAdapterError(
                "telop-text-mismatch",
                f"card {card.card_id}: existing text {readback.text!r}"
                f" != committed {binding.styled_text!r}",
            )
        rows.append(
            {
                "card_id": str(card.card_id),
                "kind": card.kind,
                "text": card.text,
                "record_span": {
                    "start_frame": card.record_span.start_frame,
                    "end_frame": card.record_span.end_frame,
                },
                "spans": [
                    {"start_frame": start - timeline_start, "end_frame": end - timeline_start}
                    for start, end in abs_spans
                ],
                "style": verify_telop_card(f"card {card.card_id}", readback, binding),
            }
        )
    if appended:
        fresh = items_in_track(ctx, TELOP_TRACK_INDEX).items
        verify_track_spans(tuple((row.start, row.end) for row in fresh), tuple(expected))
    return {"cards": rows}


__all__ = ["apply_telop"]
