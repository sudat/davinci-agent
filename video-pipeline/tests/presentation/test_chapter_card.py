"""Approved chapter-card frame render and decoded-frame structure (TDD)."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.presentation.chapter_card import (
    CANVAS_H,
    CANVAS_W,
    FONT_SIZE_PX,
    ChapterCardError,
    card_structural_report,
    ink_bounds,
    render_chapter_card,
)

TITLE = "どこにもないカメラバッグ"
FONT = Path("/System/Library/Fonts/Hiragino Sans GB.ttc")


def _font_available() -> bool:
    return FONT.is_file()


def test_card_is_black_canvas_with_one_centered_white_line() -> None:
    # Given: the approved title and the bold system Japanese font
    # When: rendering the chapter card frame
    # Then: 1920x1080 black canvas, one centered white line at 97px, no other ink
    if not _font_available():
        pytest.skip(f"bold Japanese font not found: {FONT}")
    card = render_chapter_card(TITLE, font_path=FONT)
    assert card.image.size == (CANVAS_W, CANVAS_H)
    assert card.image.mode == "RGB"
    assert card.font_size == FONT_SIZE_PX
    assert "W6" in card.font_face[1] or "W6" in card.font_face[0]
    pixels = card.image.tobytes()
    corners = (
        pixels[0:3],
        pixels[(CANVAS_W - 1) * 3 : (CANVAS_W - 1) * 3 + 3],
        pixels[(CANVAS_H - 1) * CANVAS_W * 3 : (CANVAS_H - 1) * CANVAS_W * 3 + 3],
        pixels[((CANVAS_H - 1) * CANVAS_W + CANVAS_W - 1) * 3 :][0:3],
    )
    assert all(pixel == b"\x00\x00\x00" for pixel in corners)
    left, top, right, bottom = card.text_bounds
    assert 0 < right - left < 1400, "title ink must be one compact line"
    assert 0 < bottom - top < 140, "title ink must be a single line"
    assert abs(left - (CANVAS_W - right)) <= 8, "title must be horizontally centered"
    assert abs(top - (CANVAS_H - bottom)) <= 8, "title must be vertically centered"
    assert max(pixels) >= 250, "white text ink must be present"
    inked = sum(1 for value in pixels if value > 16)
    assert inked / len(pixels) < 0.05, "no band, decoration, or logo may accompany the text"


def test_card_rejects_multiline_title() -> None:
    # Given: a title containing a newline
    # When: rendering the chapter card frame
    # Then: a typed single-line refusal, no image
    with pytest.raises(ChapterCardError, match="title-not-single-line"):
        render_chapter_card("上段\n下段", font_path=FONT)


def test_card_rejects_missing_font() -> None:
    # Given: a font path that does not exist
    # When: rendering the chapter card frame
    # Then: a typed font refusal
    with pytest.raises(ChapterCardError, match="font-not-found"):
        render_chapter_card(TITLE, font_path=Path("/nonexistent/font.ttc"))


def test_ink_bounds_finds_centered_band_and_ignores_black() -> None:
    # Given: a synthetic frame that is black except one centered white band
    # When: computing ink bounds at 8x6
    # Then: bounds match the band exactly
    w, h = 8, 6
    frame = bytearray(w * h * 3)
    for y in range(2, 4):
        for x in range(3, 6):
            frame[(y * w + x) * 3] = 255
            frame[(y * w + x) * 3 + 1] = 255
            frame[(y * w + x) * 3 + 2] = 255
    assert ink_bounds(bytes(frame), width=w, height=h) == (3, 2, 6, 4)
    assert ink_bounds(bytes(w * h * 3), width=w, height=h) is None


def test_card_structural_report_accepts_rendered_card_and_rejects_decoration() -> None:
    # Given: the rendered approved card and a variant with an extra white bar
    # When: running the decoded-frame structural check
    # Then: the rendered card passes; the decorated frame fails
    if not _font_available():
        pytest.skip(f"bold Japanese font not found: {FONT}")
    card = render_chapter_card(TITLE, font_path=FONT)
    report = card_structural_report(card.image.tobytes())
    assert report["ok"] is True
    decorated = card.image.copy()
    for x in range(200):
        for y in range(CANVAS_H):
            decorated.putpixel((x, y), (255, 255, 255))
    bad = card_structural_report(decorated.tobytes())
    assert bad["ok"] is False
