"""Native telop handler: nested cards on V3 (DESIGN telop-nested WBS-2)
plus the D second persistent layer on V4 (DESIGN D §5).

The ``apply_subtitles`` mirror for the telop kinds: resolve each card's
binding through the channel profile ``telop_style`` (the persistent_second
layer anchors below the persistent card's band), expand the long spans
into chapter-gapped tiles at the measured card length, create (or
re-open) one card timeline per card, place its spans on its track (V3;
persistent_second on V4) with one batched append per contiguous tile run,
and verify everything through ONE fresh track scan per track plus
through-nesting card readbacks. Reruns detect placed spans exactly and
never create duplicates.
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
    TELOP_SECOND_TRACK_INDEX,
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
    persistent_band_rect,
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


def _card_track(kind: str) -> int:
    """The card's placement track: V3 for every C kind, V4 for the D
    second persistent layer (DESIGN D §5 注1)."""
    if kind == "persistent_second":
        return TELOP_SECOND_TRACK_INDEX
    return TELOP_TRACK_INDEX


def _placement_spans(
    card: TelopCardPayload, gaps: tuple[tuple[int, int], ...]
) -> tuple[tuple[int, int], ...]:
    """The card's half-open placement spans (relative record frames):
    tiles for the tiled kinds, one span for opening/chapter."""
    start = card.record_span.start_frame
    end = card.record_span.end_frame
    if card.kind in ("persistent", "persistent_second"):
        return tile_spans((start, end), gaps, TELOP_TILE_FRAMES)
    if end - start > TELOP_TILE_FRAMES:
        raise LiveAdapterError(
            "telop-span-exceeds-card-length",
            f"card {card.card_id} ({card.kind}) span [{start},{end}) exceeds"
            f" the {TELOP_TILE_FRAMES}-frame card media; split into cards",
        )
    return ((start, end),)


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
    # The D second layer anchors below the persistent (L1) card's band —
    # resolve that anchor from the committed persistent card first (a
    # second-layer set without its L1 anchor refuses typed).
    anchor_card = next((card for card in p.cards if card.kind == "persistent"), None)
    anchor_band = (
        persistent_band_rect(anchor_card.text, style) if anchor_card else None
    )
    # Resolve every binding and placement span BEFORE any vendor call: an
    # unfittable text or an over-long single card refuses with Resolve
    # untouched (subtitle refuses malformed params at the same boundary).
    resolved = [
        (
            card,
            resolve_telop_binding(card.kind, card.text, style, anchor_band=anchor_band),
            _placement_spans(card, gaps),
            _card_track(card.kind),
        )
        for card in p.cards
    ]
    # Retry-state safety (subtitle precedent: a timed-out attempt can leave
    # a card CURRENT): re-select the prepared main timeline FIRST.
    set_current(ctx, main_name, "set-current-main-restore")
    rows: list[dict[str, object]] = []
    for track_index in sorted({track for *_, track in resolved}):
        ensure_telop_track(ctx, up_to=track_index)
        snapshot = items_in_track(ctx, track_index).items
        present = {
            (row.start, row.end)
            for row in snapshot
            if row.start is not None and row.end is not None
        }
        expected: list[tuple[int, int]] = []
        appended = False
        for card, binding, spans, _ in (r for r in resolved if r[3] == track_index):
            abs_spans = tuple(
                (timeline_start + start, timeline_start + end) for start, end in spans
            )
            expected.extend(abs_spans)
            card_name = telop_card_name(main_name, str(card.card_id))
            # create-or-reopen the card, then append its missing spans
            missing = [span for span in abs_spans if span not in present]
            if missing:
                if any(span in present for span in abs_spans):
                    clip_id = existing_card_media_pool_item(ctx, card_name)
                else:
                    clip_id = create_telop_card(ctx, card_name, binding)
                append_telop_spans(ctx, clip_id, missing, track_index)
                ctx.mark_timeline_mutated()
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
                    "track_index": track_index,
                    "style": verify_telop_card(f"card {card.card_id}", readback, binding),
                }
            )
        if appended:
            set_current(ctx, main_name, "set-current-main-restore")
            fresh = items_in_track(ctx, track_index).items
            verify_track_spans(tuple((row.start, row.end) for row in fresh), tuple(expected))
    return {"cards": rows}


__all__ = ["apply_telop"]
