"""Style binding and typed drift verification for native subtitle cues.

Single responsibility: what a style profile binds (font, size, center,
drop shadow) and how an independent card readback is compared against it.
Route rationale and probe evidence: see ``live_handlers/subtitle.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final, NamedTuple

from services.mcp_execution.live_handlers.common import LiveAdapterError

if TYPE_CHECKING:
    from services.creative_plan.presentation_intents import SubtitleShadowStyle

#: The axes a 2D point binding compares (Text+ "Center" x/y).
_BOUND_AXES: Final = ("1", "2")

#: Drop-shadow wire inputs on the cue Text+ (DESIGN D §02: shadow is the
#: Text+ shading ELEMENT 3 — fill is the unsuffixed pair, outline the
#: ``*2`` pair, so the shadow pair carries the ``3`` suffix). LIVE-VERIFIED
#: on the D-sample disposable project 2026-09-04 (probe.json +
#: probe-offset.json): every scalar name value-reads back; the offset is
#: the POINT input ``Offset3`` written as ``[x, y]`` — the OffsetX3/
#: OffsetY3 names are PHANTOM successes (write "succeeds", readback None).
#: Offset unit measured ≈150-230px per 1.0 (two-point fit, crescent-
#: centroid estimates): (0.025, -0.04) ≈ 6px right / 6px screen-down.
SHADOW_ENABLE_INPUT: Final = "Enabled3"
_SHADOW_INPUTS: Final = ("Red3", "Green3", "Blue3", "Alpha3", "Softness3")
SHADOW_OFFSET_INPUT: Final = "Offset3"

#: Wire precision matching the telop binding convention (the vendor echoes
#: what was set at this quantization; WBS-1 probe check-c).
_WIRE_DIGITS: Final = 4


class StyleBinding(NamedTuple):
    """One style profile resolved to concrete, font-verified Text+ inputs."""

    font: str
    size: float
    center: tuple[float, float]
    shadow_enabled: int = 0
    shadow_inputs: Mapping[str, object] = {}

    @property
    def inputs(self) -> dict[str, object]:
        """The Text+ presentation inputs this binding writes."""
        out: dict[str, object] = {"Size": self.size, "Center": [self.center[0], self.center[1]]}
        if self.shadow_enabled:
            out.update(dict(self.shadow_inputs))
        return out


def shadow_wire_inputs(shadow: SubtitleShadowStyle) -> tuple[int, dict[str, object]]:
    """Map the profile shadow block onto the Text+ element-3 wire inputs
    (enable bit + the full written input set)."""
    r_name, g_name, b_name, a_name, soft_name = _SHADOW_INPUTS

    def wire(value: float) -> float:
        return round(value, _WIRE_DIGITS)

    return 1, {
        SHADOW_ENABLE_INPUT: 1,
        r_name: wire(shadow.color[0] / 255),
        g_name: wire(shadow.color[1] / 255),
        b_name: wire(shadow.color[2] / 255),
        a_name: wire(shadow.alpha / 255),
        soft_name: wire(shadow.softness),
        SHADOW_OFFSET_INPUT: [wire(shadow.offset_x), wire(shadow.offset_y)],
    }


class CardReadback(NamedTuple):
    """Independent card-timeline readback: text plus bound style inputs."""

    text: str
    font: object
    size: object
    center: object
    shadow_enabled: object = None


def normalize_point(value: object) -> tuple[float, float] | None:
    """Resolve a Text+ point readback into one typed comparable value.

    Readback arrives as ``{"1": x, "2": y, "3": z}`` (Resolve stores points
    3D; the live-measured z is outside the 2D binding domain) while the
    request form is ``[x, y]``. Both normalize to the bound (x, y) float
    pair. A missing or non-numeric bound axis stays ``None`` so callers
    treat it as drift — points are compared by value on every bound axis,
    never by wire shape.
    """
    if isinstance(value, Mapping):
        if not all(axis in value for axis in _BOUND_AXES):
            return None
        x, y = (value[axis] for axis in _BOUND_AXES)
    elif isinstance(value, (list, tuple)) and len(value) >= len(_BOUND_AXES):
        x, y = value[0], value[1]
    else:
        return None
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return None
    if not isinstance(y, (int, float)) or isinstance(y, bool):
        return None
    return (float(x), float(y))


def verify_card_style(
    label: str, card: CardReadback, style: StyleBinding
) -> dict[str, object]:
    """Compare every bound style property with the independent readback.

    Drift in font, size, center, or the shadow-enable axis is a typed
    ``subtitle-style-mismatch``: a drifted size rescales every glyph, a
    drifted center moves them all on screen, and a shadow knocked off
    changes every glyph's silhouette — none may pass silently. Returns
    the readback-sourced style evidence for the caller's per-cue row.
    """
    center = normalize_point(card.center)
    if card.font != style.font:
        raise LiveAdapterError(
            "subtitle-style-mismatch",
            f"{label}: font readback {card.font!r} != bound {style.font!r}",
        )
    if card.size != style.size:
        raise LiveAdapterError(
            "subtitle-style-mismatch",
            f"{label}: size readback {card.size!r} != bound {style.size!r}",
        )
    if center is None or center != style.center:
        raise LiveAdapterError(
            "subtitle-style-mismatch",
            f"{label}: center readback {card.center!r} != bound {style.center!r}",
        )
    if card.shadow_enabled != style.shadow_enabled:
        raise LiveAdapterError(
            "subtitle-style-mismatch",
            f"{label}: shadow_enabled readback {card.shadow_enabled!r}"
            f" != bound {style.shadow_enabled!r}",
        )
    return {
        "font": card.font,
        "size": card.size,
        "center": [center[0], center[1]],
        "shadow_enabled": card.shadow_enabled,
    }


__all__ = [
    "SHADOW_ENABLE_INPUT",
    "CardReadback",
    "StyleBinding",
    "normalize_point",
    "shadow_wire_inputs",
    "verify_card_style",
]
