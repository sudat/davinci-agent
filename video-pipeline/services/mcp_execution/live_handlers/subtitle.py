"""Native exact-cue subtitle handler (mcp-complete-parity Task 4).

Probe-evidenced construction (capabilities/v4.4/probes/task4-native-subtitle,
Resolve 21.0.4.5, pinned MCP 2.98.3 compound):

- The auto-caption path (``timeline_ai.create_subtitles`` /
  ``timeline.subtitle_generation_probe(allow_generate=True)``) only
  normalizes autoCaptionSettings — it cannot express committed cue text or
  timing, so this handler never uses it for cue authoring.
- ``AddFusionComp`` on media clips is a measured render silent-lie and is
  not used.
- The adopted route is the vendor-measured nested timeline (mechanics in
  ``live_handlers/subtitle_card``): per cue, a card timeline holds one
  Fusion Title whose ``Template`` Text+ ``StyledText`` carries the exact
  text; the card's MediaPoolItem lands on the overlay track at the exact
  record frame with an exclusive end frame.
- The style profile binds a Japanese-capable Text+ font: the tool default
  ("Open Sans") renders committed Japanese as tofu boxes, and a bare font
  family name is a silent lie (readback echoes but the render loses its
  video stream) — the bound value must be a concrete resolvable weight
  name, verified against an independent ``get_input`` readback.

Handler order (fixed): validate → existing pinned MCP actions → typed
response parse → independent readback → product result. Reruns detect the
identical cue by exact record span and verify text plus every bound style
property (font, size, center) through the card timeline; duplicates are
never created and mismatches are typed failures.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from services.creative_plan.presentation_intents import load_default_profile
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveAdapterUnsupportedError,
    LiveSessionContext,
    validate_params,
)
from services.mcp_execution.live_handlers.subtitle_card import (
    OVERLAY_TRACK_INDEX,
    create_cue,
    ensure_overlay_track,
    items_in_track,
    read_card,
    set_current,
)
from services.mcp_execution.live_handlers.subtitle_style import (
    StyleBinding,
    verify_card_style,
)
from services.mcp_execution.plan_payloads import SubtitleParams

#: The style profile id (``SubtitleStyleProfileV1.profile_id`` default) whose
#: Text+ presentation comes from the channel presentation profile's
#: ``subtitle_style`` appearance fields (WBS-0 externalization — values
#: byte-identical to the former inline table). Any other id is a typed
#: refusal, never a silent default. Font values must be concrete resolvable
#: weight names — see the module docstring.
_SUBTITLE_STYLE_PROFILE_ID: Final = "subtitle-style-default"


def _resolve_style_binding(style_profile_id: str) -> StyleBinding:
    """Bind a style profile id through the channel presentation profile."""
    if style_profile_id != _SUBTITLE_STYLE_PROFILE_ID:
        raise LiveAdapterUnsupportedError(
            "style-profile-unsupported", f"no Text+ mapping for {style_profile_id!r}"
        )
    style = load_default_profile().subtitle_style
    return StyleBinding(font=style.font, size=style.size_screen_ratio, center=style.center)


def apply_subtitles(
    ctx: LiveSessionContext, _action: str, params: Mapping[str, object]
) -> dict[str, object]:
    """Create every committed cue natively; reruns create no duplicates."""
    p = validate_params(SubtitleParams, params)
    if p.selected_path != "native_text_plus":
        raise LiveAdapterUnsupportedError(
            "subtitle-path-not-native", f"selected_path {p.selected_path!r}"
        )
    style = _resolve_style_binding(p.style_profile_id)
    if ctx.timeline_start is None or ctx.current_timeline_name is None:
        raise LiveAdapterError("timeline-not-prepared", "prepare first")
    main_name = ctx.current_timeline_name
    timeline_start = int(ctx.timeline_start)
    # Retry-state safety (live evidence: a timed-out attempt left a cue
    # card CURRENT and track 2 read 0 items on it): every attempt FIRST
    # re-selects the prepared main timeline through the typed seam, before
    # any track-count or overlay-scan call trusts session state.
    set_current(ctx, main_name, "set-current-main-restore")
    ensure_overlay_track(ctx)
    # ONE bounded overlay-track scan for the whole rerun (live-measured:
    # 67.787 s per scan for 100 items — the old per-cue scan re-walked the
    # track once per cue). Existence answers from this snapshot are
    # monotone-safe: cue creations only ADD rows, so a cue found here stays
    # found, and a missing cue still creates — create_cue keeps its own
    # FRESH post-mutation scan and card readback for independent proof.
    snapshot = items_in_track(ctx, OVERLAY_TRACK_INDEX).items
    rows: list[dict[str, object]] = []
    for cue in p.cues:
        abs_start = timeline_start + cue.record_span.start_frame
        abs_end = timeline_start + cue.record_span.end_frame
        existing = any(row.start == abs_start and row.end == abs_end for row in snapshot)
        if existing:
            card = read_card(ctx, f"{main_name}-cue-{cue.cue_id}")
            if card.text != cue.text:
                raise LiveAdapterError(
                    "subtitle-text-mismatch",
                    f"cue {cue.cue_id}: existing text {card.text!r} != committed {cue.text!r}",
                )
            style_evidence = verify_card_style(f"cue {cue.cue_id}", card, style)
        else:
            style_evidence = create_cue(ctx, cue, style)
        rows.append(
            {
                "cue_id": cue.cue_id,
                "text": cue.text,
                "record_span": {
                    "start_frame": cue.record_span.start_frame,
                    "end_frame": cue.record_span.end_frame,
                },
                "style": style_evidence,
            }
        )
    return {"cues": rows}


__all__ = ["apply_subtitles"]
