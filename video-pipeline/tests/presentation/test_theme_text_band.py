"""Theme text band: dark translucent rectangle behind title text (TDD, Given/When/Then)."""


from __future__ import annotations

from pathlib import Path

import pytest
from PIL import ImageDraw

from services.presentation.theme_text import (
    ThemeTextFit,
    fit_theme_text,
    render_theme_text,
)

THEME = "【人生終わった】カメラのケースが見つからない件【ヤバい怒られる】"
FONT = Path("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc")
# Locked fit geometry captured before band implementation (approved values).
OPEN_FIT = ThemeTextFit(
    lines=("【人生終わった】カメラのケースが", "見つからない件【ヤバい怒られる】"),
    font_size=97,
    stroke_width=8,
    bounds=(96, 423, 1664, 656),
    variant="opening",
)
PERS_FIT = ThemeTextFit(
    lines=("【人生終わった】カメラのケースが", "見つからない件【ヤバい怒られる】"),
    font_size=38,
    stroke_width=3,
    bounds=(48, 27, 662, 119),
    variant="persistent",
)
# Expected padded band rects derived from the locked bounds.
OPEN_BAND = (73, 404, 1687, 675)
PERS_BAND = (33, 15, 677, 131)
BAND_FILL = (10, 10, 10, 140)


def _require_font() -> None:
    if not FONT.is_file():
        pytest.skip(f"bold Japanese font not found: {FONT}")


def test_exact_title_fit_geometry_locked() -> None:
    # Given: the approved exact theme title
    # When: fitting for both variants
    # Then: lines, font size, stroke width, and bounds match the locked values
    _require_font()
    for expected in (OPEN_FIT, PERS_FIT):
        fit = fit_theme_text(THEME, variant=expected.variant, font_path=FONT)
        assert fit.lines == expected.lines
        assert fit.font_size == expected.font_size
        assert fit.stroke_width == expected.stroke_width
        assert fit.bounds == expected.bounds
        assert fit.variant == expected.variant


def test_band_drawn_as_square_dark_translucent_rectangle() -> None:
    # Given: the locked fit and band rects for both variants
    # When: the render runs
    # Then: band interior pixel is exactly the dark translucent fill with square corners
    _require_font()
    for fit, band in ((OPEN_FIT, OPEN_BAND), (PERS_FIT, PERS_BAND)):
        img = render_theme_text(THEME, variant=fit.variant, font_path=FONT)
        x0, y0, x1, y1 = band
        mid_y = (y0 + y1) // 2
        inner = img.getpixel((x0 + 2, mid_y))
        assert inner == BAND_FILL
        corner = img.getpixel((x0, y0))
        assert corner == BAND_FILL
        for edge in ((x0 - 1, y0), (x0, y0 - 1), (x0 - 1, y0 - 1)):
            edge_pix = img.getpixel(edge)
            assert isinstance(edge_pix, tuple)
            assert edge_pix[3] == 0
        assert img.getpixel((x1 - 1, y1 - 1)) == BAND_FILL


def test_band_covers_text_bounds_with_modest_padding() -> None:
    # Given: locked text bounds per variant
    # When: computing the padded band rect expectations
    # Then: band equals bounds expanded by variant padding and stays inside canvas
    _require_font()
    expected_open = (
        OPEN_FIT.bounds[0] - 23, OPEN_FIT.bounds[1] - 19,
        OPEN_FIT.bounds[2] + 23, OPEN_FIT.bounds[3] + 19,
    )
    expected_pers = (
        PERS_FIT.bounds[0] - 15, PERS_FIT.bounds[1] - 12,
        PERS_FIT.bounds[2] + 15, PERS_FIT.bounds[3] + 12,
    )
    assert expected_open == OPEN_BAND
    assert expected_pers == PERS_BAND
    for band in (OPEN_BAND, PERS_BAND):
        assert 0 <= band[0] < band[2] <= 1920
        assert 0 <= band[1] < band[3] <= 1080
    assert (PERS_BAND[2] - PERS_BAND[0]) <= int(1920 * 0.42)


def test_outside_band_fully_transparent_no_placeholder() -> None:
    # Given: a rendered frame
    # When: clearing the band rect and scanning the remainder
    # Then: nothing is drawn outside the band (no stray text, no channel placeholder)
    _require_font()
    for fit, band in ((OPEN_FIT, OPEN_BAND), (PERS_FIT, PERS_BAND)):
        img = render_theme_text(THEME, variant=fit.variant, font_path=FONT)
        assert all("channel" not in line.lower() for line in fit.lines)
        outside = img.copy()
        ImageDraw.Draw(outside).rectangle(band, fill=(0, 0, 0, 0))
        assert outside.getbbox() is None


def test_text_white_no_saturated_accent_color() -> None:
    # Given: rendered band frames
    # When: scanning all nonzero pixels inside the band
    # Then: white text pixels exist and no pixel has saturated accent color
    _require_font()
    for fit, band in ((OPEN_FIT, OPEN_BAND), (PERS_FIT, PERS_BAND)):
        img = render_theme_text(THEME, variant=fit.variant, font_path=FONT)
        crop = img.crop(band)
        raw = crop.tobytes()
        has_white = False
        for i in range(0, len(raw), 4):
            r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
            if a == 0:
                continue
            assert max(r, g, b) - min(r, g, b) <= 24, f"saturated pixel {(r, g, b, a)}"
            if a == 255 and r > 200 and g > 200 and b > 200:
                has_white = True
        assert has_white, "white text not found inside band"
