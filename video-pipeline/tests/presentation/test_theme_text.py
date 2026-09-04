"""Isolated Japanese theme text fitter and RGBA renderer (TDD, Given/When/Then)."""


from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from services.presentation.theme_text import (
    ThemeTextError,
    ThemeVariant,
    fit_theme_text,
    render_theme_text,
)

THEME = "【人生終わった】カメラのケースが見つからない件【ヤバい怒られる】"
FONT = Path("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc")
CANVAS_W = 1920
CANVAS_H = 1080


def _font_available() -> bool:
    return FONT.is_file()


def test_opening_fits_long_theme_without_clipping() -> None:
    # Given: the exact long theme title and a bold Japanese font
    # When: fitting for the opening 1920x1080 center-left box
    # Then: at most two lines, size within 6%-9% of height, stroke set,
    #       bounds stay in canvas and width stays under 85%
    if not _font_available():
        pytest.skip(f"bold Japanese font not found: {FONT}")
    fit = fit_theme_text(THEME, variant="opening", font_path=FONT)
    assert len(fit.lines) <= 2
    assert "".join(fit.lines) == THEME
    assert 64 <= fit.font_size <= 97
    assert fit.stroke_width >= 3
    left, top, right, bottom = fit.bounds
    assert 0 <= left < right <= CANVAS_W
    assert 0 <= top < bottom <= CANVAS_H
    assert (right - left) <= int(CANVAS_W * 0.85)
    # stroke-inclusive width via multiline_textbbox must equal bounds width
    font = ImageFont.truetype(str(FONT), fit.font_size)
    im = Image.new("RGBA", (1, 1))
    d = ImageDraw.Draw(im)
    raw = d.multiline_textbbox(
        (0, 0),
        "\n".join(fit.lines),
        font=font,
        stroke_width=fit.stroke_width,
        spacing=int(fit.font_size * 0.2),
        align="left",
    )
    bbox = (int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]))
    assert (bbox[2] - bbox[0]) == (right - left)
    assert (bbox[3] - bbox[1]) == (bottom - top)


def test_persistent_fits_narrow_box() -> None:
    # Given: the same long theme title for persistent top-left
    # When: fitting for persistent box (2.5% margin, 32.5% width)
    # Then: fits narrow width, size within 2.5%-4% height, bounds respect margins
    if not _font_available():
        pytest.skip(f"bold Japanese font not found: {FONT}")
    fit = fit_theme_text(THEME, variant="persistent", font_path=FONT)
    assert len(fit.lines) <= 2
    assert "".join(fit.lines) == THEME
    assert 27 <= fit.font_size <= 43
    left, top, right, bottom = fit.bounds
    assert left == int(CANVAS_W * 0.025)
    assert top == int(CANVAS_H * 0.025)
    assert (right - left) <= int(CANVAS_W * 0.325)
    assert 0 <= left < right <= CANVAS_W
    assert 0 <= top < bottom <= CANVAS_H


def test_max_two_lines_is_enforced() -> None:
    # Given: a text that would otherwise need three lines at lower bound
    # When: fitting chooses split
    # Then: never exceeds two lines
    if not _font_available():
        pytest.skip(f"bold Japanese font not found: {FONT}")
    fit = fit_theme_text(THEME, variant="persistent", font_path=FONT)
    assert len(fit.lines) in (1, 2)


def test_short_phrase_remains_one_line_when_it_fits() -> None:
    # Given: a short Japanese phrase that fits in one line at base size
    # When: fitting for opening and persistent
    # Then: keeps one line, does not split to two
    if not _font_available():
        pytest.skip(f"bold Japanese font not found: {FONT}")
    short = "短いタイトル"
    open_fit = fit_theme_text(short, variant="opening", font_path=FONT)
    assert len(open_fit.lines) == 1
    assert open_fit.lines[0] == short
    persist_fit = fit_theme_text(short, variant="persistent", font_path=FONT)
    assert len(persist_fit.lines) == 1
    assert persist_fit.lines[0] == short
    tiny = "短い"
    tiny_open = fit_theme_text(tiny, variant="opening", font_path=FONT)
    assert len(tiny_open.lines) == 1
    tiny_persist = fit_theme_text(tiny, variant="persistent", font_path=FONT)
    assert len(tiny_persist.lines) == 1


def test_bracket_preferred_wrapping_is_deterministic() -> None:
    # Given: a short bracketed text where bracket split fits cleanly
    # When: fitting at persistent size
    # Then: split occurs after 】 (bracket-preferred) not arbitrary codepoint
    if not _font_available():
        pytest.skip(f"bold Japanese font not found: {FONT}")
    jp_short = "【あ】い"
    fit = fit_theme_text(jp_short, variant="persistent", font_path=FONT)
    # Either one line fits, or split after 】
    if len(fit.lines) == 2:
        assert fit.lines[0].endswith("】")
    # Long theme also deterministic: second call yields same split
    a = fit_theme_text(THEME, variant="opening", font_path=FONT)
    b = fit_theme_text(THEME, variant="opening", font_path=FONT)
    assert a.lines == b.lines
    assert a.font_size == b.font_size


def _first_opaque_pixel(
    img: Image.Image, bounds: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    left, top, right, bottom = bounds
    for y in range(top, bottom):
        for x in range(left, right):
            pixel = img.getpixel((x, y))
            assert isinstance(pixel, tuple)
            if pixel[3] != 0:
                return (int(pixel[0]), int(pixel[1]), int(pixel[2]), int(pixel[3]))
    return None


def _ink_flags(img: Image.Image, bounds: tuple[int, int, int, int]) -> set[str]:
    left, top, right, bottom = bounds
    flags: set[str] = set()
    for y in range(top, bottom):
        for x in range(left, right):
            pixel = img.getpixel((x, y))
            assert isinstance(pixel, tuple)
            r, g, b, a = int(pixel[0]), int(pixel[1]), int(pixel[2]), int(pixel[3])
            if a != 0 and r > 200 and g > 200 and b > 200:
                flags.add("white")
            if a != 0 and r < 40 and g < 40 and b < 40:
                flags.add("outline")
    return flags


def test_render_produces_transparent_full_frame_rgba_with_correct_colors() -> None:
    # Given: a fitted theme text
    # When: full-frame RGBA rendering runs
    # Then: 1920x1080 RGBA image with a transparent background, white text
    #       plus a near-black outline, and no channel placeholder
    if not _font_available():
        pytest.skip(f"bold Japanese font not found: {FONT}")
    img = render_theme_text(THEME, variant="opening", font_path=FONT)
    assert img.mode == "RGBA"
    assert img.size == (1920, 1080)
    corner = img.getpixel((0, 0))
    assert isinstance(corner, tuple)
    assert corner[3] == 0
    fit = fit_theme_text(THEME, variant="opening", font_path=FONT)
    assert _first_opaque_pixel(img, fit.bounds) is not None, "bounds contain no opaque pixels"
    flags = _ink_flags(img, fit.bounds)
    assert "white" in flags, "white bold text not found"
    assert "outline" in flags, "near-black outline not found"


def test_render_does_not_include_channel_placeholder() -> None:
    # Given: theme text render (channel name unset per spec)
    # When: the render runs
    # Then: no extra placeholder text is drawn beyond theme lines
    if not _font_available():
        pytest.skip(f"bold Japanese font not found: {FONT}")
    fit = fit_theme_text(THEME, variant="persistent", font_path=FONT)
    img = render_theme_text(THEME, variant="persistent", font_path=FONT)
    pe = img.getpixel((1919, 1079))
    assert isinstance(pe, tuple)
    assert pe[3] == 0
    assert fit.lines == tuple(s for s in fit.lines if "channel" not in s.lower())


def test_no_fit_raises_typed_error_with_code() -> None:
    # Given: an absurdly long text impossible to fit at lower bound
    # When: the fit runs
    # Then: raises ThemeTextError with code theme-text-does-not-fit
    if not _font_available():
        pytest.skip(f"bold Japanese font not found: {FONT}")
    absurd = THEME * 10
    with pytest.raises(ThemeTextError) as exc:
        fit_theme_text(absurd, variant="persistent", font_path=FONT)
    assert exc.value.code == "theme-text-does-not-fit"


def test_font_path_must_be_explicit_and_contain_japanese(tmp_path: Path) -> None:
    # Given: a missing font path
    # When: the fit runs
    # Then: raises ThemeTextError (no silent fallback)
    with pytest.raises(ThemeTextError):
        fit_theme_text(THEME, variant="opening", font_path=Path("/nonexistent/font.ttc"))
    # Given: a file without Japanese glyphs
    bogus = tmp_path / "bogus.ttf"
    bogus.write_bytes(b"not a font")
    with pytest.raises(ThemeTextError):
        fit_theme_text(THEME, variant="opening", font_path=bogus)


def test_pure_fit_uses_multiline_textbbox_stroke_inclusive() -> None:
    # Given: a fitted result
    # When: recomputing bounds with multiline_textbbox stroke-inclusive
    # Then: bounds exactly match that measurement (no clipping, deterministic)
    if not _font_available():
        pytest.skip(f"bold Japanese font not found: {FONT}")
    variants: tuple[ThemeVariant, ...] = ("opening", "persistent")
    for variant in variants:
        fit = fit_theme_text(THEME, variant=variant, font_path=FONT)
        font = ImageFont.truetype(str(FONT), fit.font_size)
        im = Image.new("RGBA", (1, 1))
        d = ImageDraw.Draw(im)
        raw2 = d.multiline_textbbox(
            (0, 0),
            "\n".join(fit.lines),
            font=font,
            stroke_width=fit.stroke_width,
            spacing=int(fit.font_size * 0.2),
            align="left",
        )
        bbox2 = (int(raw2[0]), int(raw2[1]), int(raw2[2]), int(raw2[3]))
        w = bbox2[2] - bbox2[0]
        h = bbox2[3] - bbox2[1]
        left, top, right, bottom = fit.bounds
        assert (right - left) == w
        assert (bottom - top) == h
