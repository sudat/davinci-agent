"""Style binding and typed drift verification for native subtitle cues.

Single responsibility: what a style profile binds (font, size, center) and
how an independent card readback is compared against it. Route rationale
and probe evidence: see ``live_handlers/subtitle.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, NamedTuple

from services.mcp_execution.live_handlers.common import LiveAdapterError

#: The axes a 2D point binding compares (Text+ "Center" x/y).
_BOUND_AXES: Final = ("1", "2")


class StyleBinding(NamedTuple):
    """One style profile resolved to concrete, font-verified Text+ inputs."""

    font: str
    size: float
    center: tuple[float, float]

    @property
    def inputs(self) -> dict[str, object]:
        """The Text+ presentation inputs this binding writes."""
        return {"Size": self.size, "Center": [self.center[0], self.center[1]]}


class CardReadback(NamedTuple):
    """Independent card-timeline readback: text plus bound style inputs."""

    text: str
    font: object
    size: object
    center: object


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

    Drift in font, size, or center is a typed ``subtitle-style-mismatch``:
    a drifted size rescales every glyph and a drifted center moves them all
    on screen, so none may pass silently. Returns the readback-sourced
    style evidence for the caller's per-cue row.
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
    return {"font": card.font, "size": card.size, "center": [center[0], center[1]]}


__all__ = [
    "CardReadback",
    "StyleBinding",
    "normalize_point",
    "verify_card_style",
]
