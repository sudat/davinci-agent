"""Isolated Japanese theme text fitter and RGBA renderer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from PIL import Image, ImageDraw, ImageFont

_CANVAS_W: Final = 1920
_CANVAS_H: Final = 1080
_OPEN_X: Final = int(_CANVAS_W * 0.05)
_OPEN_W: Final = int(_CANVAS_W * 0.85)
_OPEN_BASE: Final = int(_CANVAS_H * 0.09)
_OPEN_MIN: Final = int(_CANVAS_H * 0.06)
_PERS_X: Final = int(_CANVAS_W * 0.025)
_PERS_Y: Final = int(_CANVAS_H * 0.025)
_PERS_W: Final = int(_CANVAS_W * 0.325)
_PERS_BASE: Final = int(_CANVAS_H * 0.04)
_PERS_MIN: Final = int(_CANVAS_H * 0.025)
# full-width punctuation that may not end a wrapped line
_PUNCT: Final = frozenset(
    "\u3001\u3002\u30fb\uff0c\uff0e\uff01\uff1f\u300d\u300f\u3009\u300b\uff09"
    "\u003a\uff1a\uff1b\u301c\u30fc\u2015\u3001\u3002\u300c\u300d"
)
# Dark translucent band drawn behind title text (render-only; fit values unchanged).
_BAND_FILL: Final[tuple[int, int, int, int]] = (10, 10, 10, 140)

ThemeVariant = Literal["opening", "persistent"]
_BAND_PADDING: Final[dict[ThemeVariant, tuple[int, int]]] = {
    "opening": (int(_CANVAS_W * 0.012), int(_CANVAS_H * 0.018)),
    "persistent": (int(_CANVAS_W * 0.008), int(_CANVAS_H * 0.012)),
}


class ThemeTextError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ThemeTextFit:
    lines: tuple[str, ...]
    font_size: int
    stroke_width: int
    bounds: tuple[int, int, int, int]
    variant: ThemeVariant


type _MeasuredFit = tuple[tuple[int, int, int, int], tuple[int, int]]


@dataclass(frozen=True, slots=True)
class _VariantBox:
    """The layout rectangle and font-size ladder for one theme variant."""

    x: int
    y: int
    w: int
    h: int
    base_size: int
    min_size: int
    center_vertically: bool


@dataclass(frozen=True, slots=True)
class _SizeContext:
    """Everything the fit helpers need at one candidate font size."""

    variant: ThemeVariant
    box: _VariantBox
    draw: ImageDraw.ImageDraw
    font: ImageFont.FreeTypeFont
    size: int
    stroke: int
    spacing: int


def _stroke_for(size: int) -> int:
    return max(3, round(size * 0.08))


def _verify_font(font_path: Path) -> None:
    if not font_path.is_file():
        raise ThemeTextError("font-not-found", f"bold Japanese font missing: {font_path}")
    try:
        font = ImageFont.truetype(str(font_path), 48)
    except OSError as error:
        raise ThemeTextError("font-invalid", f"cannot load font {font_path}: {error}") from error
    box = font.getbbox("【")
    if box is None or (box[2] - box[0]) == 0:
        raise ThemeTextError("font-missing-japanese", f"font lacks Japanese glyphs: {font_path}")
    box2 = font.getbbox("あ")
    if box2 is None or (box2[2] - box2[0]) == 0:
        raise ThemeTextError("font-missing-japanese", f"font lacks Japanese glyphs: {font_path}")


def _tier(ch: str) -> int:
    if ch == "】":
        return 0
    if ch in _PUNCT:
        return 1
    return 2


def _variant_box(variant: ThemeVariant) -> _VariantBox:
    if variant == "opening":
        return _VariantBox(
            x=_OPEN_X, y=0, w=_OPEN_W, h=_CANVAS_H,
            base_size=_OPEN_BASE, min_size=_OPEN_MIN, center_vertically=True,
        )
    if variant == "persistent":
        return _VariantBox(
            x=_PERS_X, y=_PERS_Y, w=_PERS_W,
            h=_CANVAS_H - _PERS_Y - int(_CANVAS_H * 0.025),
            base_size=_PERS_BASE, min_size=_PERS_MIN, center_vertically=False,
        )
    raise ThemeTextError("invalid-variant", f"unknown variant: {variant}")


def _measure(draw: ImageDraw.ImageDraw, text: str, ctx: _SizeContext) -> tuple[int, int, int, int]:
    raw = draw.multiline_textbbox(
        (0, 0), text, font=ctx.font, stroke_width=ctx.stroke,
        spacing=ctx.spacing, align="left",
    )
    return (int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]))


def _placement(ctx: _SizeContext, w: int, h: int) -> tuple[int, int] | None:
    left = ctx.box.x
    top = (_CANVAS_H - h) // 2 if ctx.box.center_vertically else ctx.box.y
    if left + w > _CANVAS_W or top + h > _CANVAS_H:
        return None
    return left, top


def _fit_bounds(
    ctx: _SizeContext,
    lines: tuple[str, ...] | list[str],
    bbox: tuple[int, int, int, int],
    placement: tuple[int, int],
) -> ThemeTextFit:
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    left, top = placement
    return ThemeTextFit(
        lines=tuple(lines), font_size=ctx.size, stroke_width=ctx.stroke,
        bounds=(left, top, left + w, top + h), variant=ctx.variant,
    )


def _single_line_fit(text: str, ctx: _SizeContext) -> ThemeTextFit | None:
    bbox = _measure(ctx.draw, text, ctx)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if w > ctx.box.w or h > ctx.box.h:
        return None
    placement = _placement(ctx, w, h)
    if placement is None:
        return None
    return _fit_bounds(ctx, (text,), bbox, placement)


def _split_candidates(text: str) -> list[tuple[int, int, list[str]]]:
    return [
        (_tier(text[i - 1]), i, [text[:i], text[i:]])
        for i in range(1, len(text))
    ]


def _split_line_fit(text: str, ctx: _SizeContext) -> ThemeTextFit | None:
    fitting: list[tuple[int, int, int, int, list[str], _MeasuredFit]] = []
    for tier, idx, lines in _split_candidates(text):
        bbox = _measure(ctx.draw, "\n".join(lines), ctx)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w > ctx.box.w or h > ctx.box.h:
            continue
        r0 = ctx.draw.textbbox((0, 0), lines[0], font=ctx.font, stroke_width=ctx.stroke)
        r1 = ctx.draw.textbbox((0, 0), lines[1], font=ctx.font, stroke_width=ctx.stroke)
        w0, w2 = int(r0[2]) - int(r0[0]), int(r1[2]) - int(r1[0])
        placement = _placement(ctx, w, h)
        if placement is None:
            continue
        fitting.append((tier, max(w0, w2), abs(w0 - w2), idx, lines, (bbox, placement)))
    if not fitting:
        return None
    fitting.sort(key=lambda item: item[:4])
    _, _, _, _, best_lines, (best_bbox, best_placement) = fitting[0]
    return _fit_bounds(ctx, best_lines, best_bbox, best_placement)


def _size_context(
    variant: ThemeVariant, font_path: Path, box: _VariantBox, size: int
) -> _SizeContext:
    try:
        font = ImageFont.truetype(str(font_path), size)
    except OSError as error:
        raise ThemeTextError("font-invalid", f"cannot load font at {size}: {error}") from error
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    return _SizeContext(
        variant=variant, box=box, draw=probe, font=font,
        size=size, stroke=_stroke_for(size), spacing=int(size * 0.2),
    )


def fit_theme_text(text: str, *, variant: ThemeVariant, font_path: Path) -> ThemeTextFit:
    _verify_font(font_path)
    box = _variant_box(variant)
    for size in range(box.base_size, box.min_size - 1, -1):
        ctx = _size_context(variant, font_path, box, size)
        fit = _single_line_fit(text, ctx)
        if fit is not None:
            return fit
        fit = _split_line_fit(text, ctx)
        if fit is not None:
            return fit
    raise ThemeTextError(
        "theme-text-does-not-fit",
        f"text does not fit {variant} at lower bound {box.min_size}: {text[:30]!r}",
    )


def _band_rect(fit: ThemeTextFit) -> tuple[int, int, int, int]:
    pad_x, pad_y = _BAND_PADDING[fit.variant]
    left, top, right, bottom = fit.bounds
    return (
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(_CANVAS_W, right + pad_x),
        min(_CANVAS_H, bottom + pad_y),
    )


def render_theme_text(text: str, *, variant: ThemeVariant, font_path: Path) -> Image.Image:
    fit = fit_theme_text(text, variant=variant, font_path=font_path)
    img = Image.new("RGBA", (_CANVAS_W, _CANVAS_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle(_band_rect(fit), fill=_BAND_FILL)
    font = ImageFont.truetype(str(font_path), fit.font_size)
    txt = "\n".join(fit.lines)
    sp = int(fit.font_size * 0.2)
    stroke = max(2, fit.stroke_width // 2)
    raw_tmp = draw.multiline_textbbox(
        (0, 0), txt, font=font, stroke_width=stroke, spacing=sp, align="left"
    )
    tmp = (int(raw_tmp[0]), int(raw_tmp[1]), int(raw_tmp[2]), int(raw_tmp[3]))
    left, top = fit.bounds[0], fit.bounds[1]
    draw.multiline_text(
        (left - tmp[0], top - tmp[1]), txt, font=font, fill=(255, 255, 255, 255),
        stroke_width=stroke, stroke_fill=(16, 16, 16, 255), align="left", spacing=sp,
    )
    return img
