"""Approved chapter card: black canvas, one centered bold white line, nothing else."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

CANVAS_W: Final = 1920
CANVAS_H: Final = 1080
FONT_SIZE_PX: Final = 97
# The pinned bold face inside the macOS Hiragino collection (index 2 = "W6").
FONT_INDEX_W6: Final = 2
INK_THRESHOLD: Final = 16
MAX_LINE_HEIGHT: Final = 140
MAX_CENTERING_ERROR: Final = 16
MAX_INK_FRACTION: Final = 0.05


class ChapterCardError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ChapterCardFrame:
    image: Image.Image
    font_path: Path
    font_face: tuple[str, str]
    font_size: int
    text: str
    text_bounds: tuple[int, int, int, int]


def _luma(frame: bytes | memoryview, *, width: int, height: int) -> Image.Image:
    return Image.frombytes("RGB", (width, height), frame).convert("L")


def _ink_mask(
    frame: bytes | memoryview, *, width: int, height: int, ink_threshold: int
) -> Image.Image:
    def _over(value: int) -> int:
        return 255 if value > ink_threshold else 0

    return _luma(frame, width=width, height=height).point(_over, mode="L")


def ink_bounds(
    frame: bytes | memoryview, *, width: int, height: int, ink_threshold: int = INK_THRESHOLD
) -> tuple[int, int, int, int] | None:
    """Tight bounding box of inked pixels; None when the frame carries no ink."""

    bounds = _ink_mask(frame, width=width, height=height, ink_threshold=ink_threshold).getbbox()
    return bounds if bounds is not None else None


def ink_fraction(
    frame: bytes | memoryview, *, width: int, height: int, ink_threshold: int = INK_THRESHOLD
) -> float:
    histogram = _luma(frame, width=width, height=height).histogram()
    inked = sum(histogram[ink_threshold + 1 :])
    return inked / (width * height)


def render_chapter_card(title: str, *, font_path: Path) -> ChapterCardFrame:
    """Render the approved card: pure black, one centered white line, no stroke/band."""

    if not title or "\n" in title or "\r" in title or not title.strip():
        raise ChapterCardError(
            "title-not-single-line", f"title must be one non-empty line: {title!r}"
        )
    if not font_path.is_file():
        raise ChapterCardError("font-not-found", f"bold Japanese font missing: {font_path}")
    try:
        font = ImageFont.truetype(str(font_path), FONT_SIZE_PX, index=FONT_INDEX_W6)
    except OSError as error:
        raise ChapterCardError("font-invalid", f"cannot load font {font_path}: {error}") from error
    probe = font.getbbox(title[:1] or " ")
    if probe is None or (probe[2] - probe[0]) == 0:
        raise ChapterCardError("font-missing-japanese", f"font lacks usable glyphs: {font_path}")
    image = Image.new("RGB", (CANVAS_W, CANVAS_H), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    left, top, raw_right, raw_bottom = draw.textbbox((0, 0), title, font=font)
    width, height = raw_right - left, raw_bottom - top
    if not 0 < width < CANVAS_W or not 0 < height < MAX_LINE_HEIGHT:
        raise ChapterCardError(
            "title-does-not-fit",
            f"title ink is {width}x{height} at {FONT_SIZE_PX}px; expected one line under "
            f"{MAX_LINE_HEIGHT}px on {CANVAS_W}x{CANVAS_H}",
        )
    base_x, base_y = (CANVAS_W - width) // 2 - left, (CANVAS_H - height) // 2 - top
    scratch = Image.new("RGB", (CANVAS_W, CANVAS_H), (0, 0, 0))
    ImageDraw.Draw(scratch).text((base_x, base_y), title, font=font, fill=(255, 255, 255))
    scratch_ink = ink_bounds(scratch.tobytes(), width=CANVAS_W, height=CANVAS_H)
    if scratch_ink is None:
        raise ChapterCardError("card-blank", "rendered card carries no ink")
    ink_left, ink_top, ink_right, ink_bottom = scratch_ink
    draw.text(
        (
            base_x + (CANVAS_W - (ink_right - ink_left)) // 2 - ink_left,
            base_y + (CANVAS_H - (ink_bottom - ink_top)) // 2 - ink_top,
        ),
        title,
        font=font,
        fill=(255, 255, 255),
    )
    bounds = ink_bounds(image.tobytes(), width=CANVAS_W, height=CANVAS_H)
    if bounds is None:
        raise ChapterCardError("card-blank", "rendered card carries no ink")
    face = font.getname()
    return ChapterCardFrame(
        image=image,
        font_path=font_path,
        font_face=(str(face[0]), str(face[1])),
        font_size=FONT_SIZE_PX,
        text=title,
        text_bounds=bounds,
    )


def card_structural_report(
    frame: bytes | memoryview,
    *,
    ink_threshold: int = INK_THRESHOLD,
    centering_error: int = MAX_CENTERING_ERROR,
) -> dict[str, object]:
    """Decoded-frame check: black field, one compact centered ink line, sparse ink.

    ``ink_threshold`` defaults suit the pristine render; decoded MOV frames need
    a higher threshold (limited-range black decodes to ~16, white to ~235).
    """

    bounds = ink_bounds(frame, width=CANVAS_W, height=CANVAS_H, ink_threshold=ink_threshold)
    if bounds is None:
        return {"ok": False, "reason": "no-ink", "bounds": None, "ink_fraction": 0.0}
    left, top, right, bottom = bounds
    fraction = ink_fraction(frame, width=CANVAS_W, height=CANVAS_H, ink_threshold=ink_threshold)
    reasons: list[str] = []
    if bottom - top >= MAX_LINE_HEIGHT:
        reasons.append(f"ink-height-{bottom - top}")
    if abs(left - (CANVAS_W - right)) > centering_error:
        reasons.append(f"not-centered-x-{left}-{right}")
    if abs(top - (CANVAS_H - bottom)) > centering_error:
        reasons.append(f"not-centered-y-{top}-{bottom}")
    if fraction >= MAX_INK_FRACTION:
        reasons.append(f"ink-fraction-{fraction:.4f}")
    return {
        "ok": not reasons,
        "reason": ";".join(reasons),
        "bounds": list(bounds),
        "ink_fraction": fraction,
    }


__all__ = [
    "CANVAS_H",
    "CANVAS_W",
    "FONT_INDEX_W6",
    "FONT_SIZE_PX",
    "ChapterCardError",
    "ChapterCardFrame",
    "card_structural_report",
    "ink_bounds",
    "ink_fraction",
    "render_chapter_card",
]
