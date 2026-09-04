"""Profile-sourced telop card bindings and typed drift verification.

Single responsibility (mirror of ``subtitle_style``): how one telop card's
kind + text + the channel profile's ``telop_style`` resolve to the band
template's 19 probe-proven published inputs, and how an independent card
readback compares against that binding. Geometry comes from the
authoritative calculator ``presentation/theme_text.py`` (``fit_theme_text``
— wrapping and the font-size ladder are NOT re-implemented here); the
chapter card has no band/outline so it binds the reduced 7-input set.
Wire values are rounded to the precision the WBS-1 probe proved to
round-trip on Resolve 21.0.4.5 (check-c).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveAdapterUnsupportedError,
)
from services.mcp_execution.live_handlers.subtitle_style import normalize_point
from services.presentation.theme_text import (
    ThemeTextError,
    ThemeVariant,
    fit_theme_text,
)

if TYPE_CHECKING:
    from services.creative_plan.presentation_intents import TelopOutline, TelopStyle

#: The deliverable canvas (``theme_text.py`` owns the same numbers; the
#: profile's boxes/pads are pixels on this canvas). Band geometry and
#: positions are normalized against THIS canvas (band matched the master
#: within 1px in the r4 diagnosis) — but ``Size``/``OutlineThickness``
#: are NOT (see ``_TEMPLATE_SIZE_UNIT_PX``).
_CANVAS_W: Final = 1920
_CANVAS_H: Final = 1080

#: The band template's ``Size``/``OutlineThickness`` input unit is a
#: ~1536.7px reference, NOT the 1080px canvas: same-glyph probe cards
#: measured glyph pitch 154/154/153px at Size 0.1 → k_mean 1536.7
#: px/unit-size (1080/1536.7 = 0.703 multiplier), so dividing profile
#: px by 1080 rendered text 1.42x too large and clipped both edges
#: (opening line widths 1920/1888px full-bleed vs master 1484/1483px;
#: clip8_L/R up to 277/628 ink px). Proven fix: Size 0.0635 = 97/1536.7
#: test card matched the master (line widths 1483/1485 vs 1484/1483,
#: glyph height 93px exact, zero edge ink). Evidence:
#: ``private/runtime/sol-size-diag-20260904/measurements-final.json``
#: (``k_calibration`` block: k_mean 1536.7, ratio 1.4228; fix probe
#: ``probe-fix-open-f00030``).
_TEMPLATE_SIZE_UNIT_PX: Final = 1536.7

#: Wire precision proven by the WBS-1 probe round-trip (0.0352 / 0.1849 /
#: 0.549 …) — the vendor echoes what was set at this quantization.
_WIRE_DIGITS: Final = 4

#: Wire ``CharacterSpacing`` written on every telop card: 1.0, the
#: stock Fusion default (write-path bisect 2026-09-04,
#: ``private/runtime/sol-writepath-probe-20260904/evidence.json``).
#: The old 0.2 was the PIL tracking coefficient from ``theme_text.py``
#: (``int(size * 0.2)`` extra pixels between wrapped LINES) mis-mapped
#: onto Fusion ``CharacterSpacing``, whose unit differs: on this
#: Resolve 0.2 collapses the text into a vertical stack (~112px-class
#: spread) and 0 squeezes it (~572px-class), while 1.0 + the
#: white-removal prefix renders master parity (~1400px-class). The PIL
#: coefficient stays authoritative for the offline fit in
#: ``theme_text.py``; the visual gate judges the final rendered look.
_CHARACTER_SPACING: Final = 1.0

#: Profile Fusion font name → the font FILE the PIL fit measures through
#: (the approved bold face, DESIGN §1.2). Unknown names refuse typed.
_FONT_FILES: Final[dict[str, Path]] = {
    "Hiragino Sans W6": Path("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"),
}


class TelopCardBinding(NamedTuple):
    """One telop card resolved to its committed Template inputs."""

    styled_text: str
    font: str
    size: float
    text_pos: tuple[float, float]
    inputs: dict[str, object]


class TelopCardReadback(NamedTuple):
    """Independent card-timeline readback: text plus bound style inputs."""

    text: str
    font: object
    size: object
    text_pos: object


def _wire(value: float) -> float:
    return round(value, _WIRE_DIGITS)


def _fusion_y(y_topdown: float) -> float:
    """Map a top-down canvas Y fraction onto Fusion's bottom-up Y axis.

    Fusion Text+/mask normalized Y is BOTTOM-origin (0 = bottom, 1 = top).
    r3 evidence (``private/runtime/sol-c-variant-r3-20260904/evidence.json``
    honest_failure, composites/diag-persistent-position-f00120.png): the
    persistent telop written with the RAW top-down fraction (TextPos /
    BandPos y = 73/1080 ≈ 0.0676) rendered BOTTOM-left — render f120
    top-left 0 / bottom-left 2080 ink px vs master top-left 1336 /
    bottom-left 0 (f7800: 0/2118 vs 1330/0) — and the standalone card
    render reproduced the flip with correct input readbacks, so it is the
    axis convention, not the values. The subtitle precedent agrees:
    subtitle center y = 0.14 renders near the BOTTOM. Opening (~0.4995)
    and chapter (0.5) sit on the flip's fixed point, which is why every
    earlier probe looked correct (r3 evidence diagnosis).
    """
    return _wire(1.0 - y_topdown)


def _font_file(font: str) -> Path:
    path = _FONT_FILES.get(font)
    if path is None:
        raise LiveAdapterUnsupportedError(
            "telop-font-file-unresolved",
            f"no measurement font file mapped for Fusion font {font!r}",
        )
    return path


def _outline_inputs(size_px: int, outline: TelopOutline) -> dict[str, object]:
    """The render-time stroke rule (theme ``_stroke_for`` halved, floored)
    expressed through the profile's ratio/ floor."""
    width_px = max(outline.min_px, round(size_px * outline.width_ratio))
    return {
        "OutlineEnabled": 1,
        "OutlineThickness": _wire(width_px / _TEMPLATE_SIZE_UNIT_PX),
        "OutlineSoftness": 0,
        "OutlineR": _wire(outline.color[0] / 255),
        "OutlineG": _wire(outline.color[1] / 255),
        "OutlineB": _wire(outline.color[2] / 255),
        "OutlineA": 1.0,
    }


def _center(bounds: tuple[int, int, int, int]) -> tuple[float, float]:
    left, top, right, bottom = bounds
    return ((left + right) / 2, (top + bottom) / 2)


def _fitted_binding(kind: str, text: str, style: TelopStyle) -> TelopCardBinding:
    appearance = style.persistent
    variant: ThemeVariant = "persistent"
    if kind == "opening":
        appearance = style.opening
        variant = "opening"
    try:
        fit = fit_theme_text(text, variant=variant, font_path=_font_file(style.font))
    except ThemeTextError as error:
        raise LiveAdapterError("telop-text-unfittable", str(error)) from error
    left, top, right, bottom = fit.bounds
    band = appearance.band
    band_left = max(0, left - band.pad_x)
    band_top = max(0, top - band.pad_y)
    band_right = min(_CANVAS_W, right + band.pad_x)
    band_bottom = min(_CANVAS_H, bottom + band.pad_y)
    text_x, text_y = _center(fit.bounds)
    band_x, band_y = _center((band_left, band_top, band_right, band_bottom))
    styled = "\n".join(fit.lines)
    outline = appearance.outline
    pos_x, pos_y = _wire(text_x / _CANVAS_W), _fusion_y(text_y / _CANVAS_H)
    wire_size = _wire(fit.font_size / _TEMPLATE_SIZE_UNIT_PX)
    inputs: dict[str, object] = {
        "StyledText": styled,
        "Font": style.font,
        "Size": wire_size,
        "TextPos": [pos_x, pos_y],
        "CharacterSpacing": _CHARACTER_SPACING,
        **_outline_inputs(fit.font_size, outline),
        "BandR": _wire(band.fill[0] / 255),
        "BandG": _wire(band.fill[1] / 255),
        "BandB": _wire(band.fill[2] / 255),
        "BandAlpha": _wire(band.alpha / 255),
        "BandWidth": _wire((band_right - band_left) / _CANVAS_W),
        "BandHeight": _wire((band_bottom - band_top) / _CANVAS_H),
        "BandPos": [_wire(band_x / _CANVAS_W), _fusion_y(band_y / _CANVAS_H)],
    }
    return TelopCardBinding(
        styled_text=styled,
        font=style.font,
        size=wire_size,
        text_pos=(pos_x, pos_y),
        inputs=inputs,
    )


def _chapter_binding(text: str, size_px: int, font: str) -> TelopCardBinding:
    wire_size = _wire(size_px / _TEMPLATE_SIZE_UNIT_PX)
    center_y = _fusion_y(0.5)
    inputs: dict[str, object] = {
        "StyledText": text,
        "Font": font,
        "Size": wire_size,
        # 0.5 is the flip's fixed point; still written via _fusion_y so the
        # bottom-up convention holds for every kind (r3: do not regress to 0.5).
        "TextPos": [0.5, center_y],
        "CharacterSpacing": _CHARACTER_SPACING,
        "OutlineEnabled": 0,
        "BandAlpha": 0.0,
    }
    return TelopCardBinding(
        styled_text=text,
        font=font,
        size=wire_size,
        text_pos=(0.5, center_y),
        inputs=inputs,
    )


def resolve_telop_binding(kind: str, text: str, style: TelopStyle) -> TelopCardBinding:
    """Resolve one card's Template inputs from the profile telop style."""
    match kind:
        case "opening" | "persistent":
            return _fitted_binding(kind, text, style)
        case "chapter":
            return _chapter_binding(text, style.chapter.size_px, style.font)
        case _:
            raise LiveAdapterUnsupportedError(
                "telop-kind-unsupported", f"no card mapping for kind {kind!r}"
            )


def verify_telop_card(
    label: str, card: TelopCardReadback, binding: TelopCardBinding
) -> dict[str, object]:
    """Compare every bound style axis with the independent readback.

    Drift in font, size, or text position is a typed
    ``telop-style-mismatch``; the remaining published inputs are verified
    by the ``safe_set_inputs`` inline readback at creation (probe check-c).
    """
    text_pos = normalize_point(card.text_pos)
    if card.font != binding.font:
        raise LiveAdapterError(
            "telop-style-mismatch",
            f"{label}: font readback {card.font!r} != bound {binding.font!r}",
        )
    if card.size != binding.size:
        raise LiveAdapterError(
            "telop-style-mismatch",
            f"{label}: size readback {card.size!r} != bound {binding.size!r}",
        )
    if text_pos is None or text_pos != binding.text_pos:
        raise LiveAdapterError(
            "telop-style-mismatch",
            f"{label}: text_pos readback {card.text_pos!r} != bound {binding.text_pos!r}",
        )
    return {
        "font": card.font,
        "size": card.size,
        "text_pos": [text_pos[0], text_pos[1]],
    }


__all__ = [
    "TelopCardBinding",
    "TelopCardReadback",
    "resolve_telop_binding",
    "verify_telop_card",
]
