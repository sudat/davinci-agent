"""WBS-2 profile→card-input mapping locks (mirror of subtitle_style_profile).

The persistent mapping is locked against the MEASURED episode geometry
(``private/runtime/__fvp_test__persistent_stack_20260903-022311`` evidence:
font_size 38, text bounds [48,27,662,119], band rect [33,15,677,131]) and
the WBS-1 probe check-c wire values (19 published inputs round-trip on
Resolve 21.0.4.5). Every written value must come from
``load_default_profile().telop_style`` — no inline style constants.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from services.creative_plan.presentation_intents import load_default_profile
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveAdapterUnsupportedError,
)
from services.mcp_execution.live_handlers.telop_style import (
    _TEMPLATE_SIZE_UNIT_PX,
    TelopCardBinding,
    TelopCardReadback,
    resolve_telop_binding,
    verify_telop_card,
)
from services.presentation.theme_text import fit_theme_text

#: The representative episode title (measured persistent-telop text).
EPISODE_TITLE = "【人生終わった】カメラのケースが見つからない件【ヤバい怒られる】"

#: macOS system font the profile's Fusion font name measures through.
W6_TTC = Path("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc")

requires_w6 = pytest.mark.skipif(not W6_TTC.is_file(), reason="system Hiragino W6 font unavailable")


@requires_w6
def test_persistent_binding_reproduces_the_measured_wire_values() -> None:
    # Given: the profile telop_style and the measured episode title
    # When: resolving the persistent card binding
    # Then: the 19 published inputs equal the probe-verified wire values
    binding = resolve_telop_binding("persistent", EPISODE_TITLE, load_default_profile().telop_style)
    assert set(binding.inputs) == {
        "StyledText",
        "Font",
        "Size",
        "TextPos",
        "CharacterSpacing",
        "OutlineEnabled",
        "OutlineThickness",
        "OutlineSoftness",
        "OutlineR",
        "OutlineG",
        "OutlineB",
        "OutlineA",
        "BandR",
        "BandG",
        "BandB",
        "BandAlpha",
        "BandWidth",
        "BandHeight",
        "BandPos",
    }
    assert binding.inputs["StyledText"] == (
        "【人生終わった】カメラのケースが\n見つからない件【ヤバい怒られる】"
    )
    assert binding.inputs["Font"] == "Hiragino Sans W6"
    # 38px measured → 38/1536.7 on the wire (r5 size fix: the template's
    # Size unit is a 1536.7px reference, not the 1080px canvas — dividing
    # by 1080 rendered 1.42x too large and clipped both edges;
    # sol-size-diag-20260904 measurements-final.json k_calibration)
    assert binding.inputs["Size"] == 0.0247
    # text bounds [48,27,662,119] → center (355,73); x 355/1920 = 0.1849,
    # y flipped to Fusion bottom-up: 1 - 73/1080 = 0.9324 (r3 evidence:
    # the raw 0.0676 rendered the persistent telop BOTTOM-left)
    assert binding.inputs["TextPos"] == [0.1849, 0.9324]
    # 1.0 = stock Fusion default; the old 0.2 (PIL line-tracking
    # coefficient) collapsed the render (write-path bisect
    # 2026-09-04, sol-writepath-probe: P2 collapsed vs P3 master parity)
    assert binding.inputs["CharacterSpacing"] == 1.0
    # outline: max(min_px=2, round(38 * 0.04)) = 2px → 2/1536.7 (r5: the
    # outline thickness shares the template's 1536.7px Size-unit reference)
    assert binding.inputs["OutlineEnabled"] == 1
    assert binding.inputs["OutlineThickness"] == round(2 / _TEMPLATE_SIZE_UNIT_PX, 4)
    assert binding.inputs["OutlineSoftness"] == 0
    assert binding.inputs["OutlineR"] == 0.0627  # 16/255
    assert binding.inputs["OutlineG"] == 0.0627
    assert binding.inputs["OutlineB"] == 0.0627
    assert binding.inputs["OutlineA"] == 1.0
    # band: fill (10,10,10) alpha 140; rect [33,15,677,131] → 644 x 116 px
    assert binding.inputs["BandR"] == 0.0392  # 10/255
    assert binding.inputs["BandG"] == 0.0392
    assert binding.inputs["BandB"] == 0.0392
    assert binding.inputs["BandAlpha"] == 0.549  # 140/255
    assert binding.inputs["BandWidth"] == 0.3354  # 644/1920
    assert binding.inputs["BandHeight"] == 0.1074  # 116/1080
    assert binding.inputs["BandPos"] == [0.1849, 0.9324]
    assert binding.text_pos == (0.1849, 0.9324)


@requires_w6
def test_fusion_y_convention_is_bottom_up_for_every_card_kind() -> None:
    # Given: r3 render evidence — the persistent telop written with a raw
    #        top-down Y rendered BOTTOM-left (render f120 top-left 0 /
    #        bottom-left 2080 ink px vs master top-left 1336 / bottom-left 0;
    #        private/runtime/sol-c-variant-r3-20260904/evidence.json)
    # When: resolving every card kind's binding
    # Then: TextPos/BandPos Y = 1 - top-down fraction: the persistent
    #       top-region card (text top edge 27px, center 73px) maps above
    #       0.9 — a top-down 27px/1080 (~0.025) or 73px/1080 (~0.0676)
    #       never reaches the wire raw; chapter keeps the 0.5 fixed point
    profile = load_default_profile()
    persistent = resolve_telop_binding("persistent", EPISODE_TITLE, profile.telop_style)
    opening = resolve_telop_binding("opening", EPISODE_TITLE, profile.telop_style)
    chapter = resolve_telop_binding("chapter", "どこにもないカメラバッグ", profile.telop_style)

    fit = fit_theme_text(EPISODE_TITLE, variant="persistent", font_path=W6_TTC)
    assert fit.bounds[1] == 27  # the measured top edge (27px/1080 ~ 0.025)
    center_y_px = (fit.bounds[1] + fit.bounds[3]) / 2  # 73
    flipped_center_y = round(1 - center_y_px / 1080, 4)
    text_pos = cast("list[float]", persistent.inputs["TextPos"])
    band_pos = cast("list[float]", persistent.inputs["BandPos"])
    assert text_pos[1] == flipped_center_y
    assert band_pos[1] == flipped_center_y
    assert text_pos[1] > 0.9
    assert text_pos[1] not in (round(27 / 1080, 4), round(center_y_px / 1080, 4))

    ofit = fit_theme_text(EPISODE_TITLE, variant="opening", font_path=W6_TTC)
    opening_center_y = (ofit.bounds[1] + ofit.bounds[3]) / 2
    assert cast("list[float]", opening.inputs["TextPos"])[1] == round(
        1 - opening_center_y / 1080, 4)
    assert cast("list[float]", opening.inputs["BandPos"])[1] == round(
        1 - opening_center_y / 1080, 4)

    assert cast("list[float]", chapter.inputs["TextPos"])[1] == 0.5  # 1 - 0.5 fixed point


@requires_w6
def test_opening_binding_is_profile_sourced_not_a_literal_copy() -> None:
    # Given: a profile whose opening band/outline differ from the defaults
    # When: resolving the opening binding
    # Then: every changed value lands in the written inputs — proving the
    #       profile is the source, not a coincidental literal
    profile = load_default_profile()
    style = profile.telop_style.model_copy(
        update={
            "opening": profile.telop_style.opening.model_copy(
                update={
                    "band": profile.telop_style.opening.band.model_copy(
                        update={"fill": (20, 30, 40), "alpha": 200, "pad_x": 40, "pad_y": 25}
                    ),
                    "outline": profile.telop_style.opening.outline.model_copy(
                        update={"color": (40, 50, 60), "width_ratio": 0.1, "min_px": 3}
                    ),
                }
            )
        }
    )
    binding = resolve_telop_binding("opening", EPISODE_TITLE, style)
    assert binding.inputs["BandR"] == round(20 / 255, 4)
    assert binding.inputs["BandG"] == round(30 / 255, 4)
    assert binding.inputs["BandB"] == round(40 / 255, 4)
    assert binding.inputs["BandAlpha"] == round(200 / 255, 4)
    assert binding.inputs["OutlineR"] == round(40 / 255, 4)
    size_px = round(binding.size * _TEMPLATE_SIZE_UNIT_PX)
    assert binding.inputs["OutlineThickness"] == round(
        max(3, round(size_px * 0.1)) / _TEMPLATE_SIZE_UNIT_PX, 4
    )
    # And: the band box follows the mutated padding around the SAME fit
    fit = fit_theme_text(EPISODE_TITLE, variant="opening", font_path=W6_TTC)
    left, top, right, bottom = fit.bounds
    assert binding.inputs["BandWidth"] == round((right + 40 - (left - 40)) / 1920, 4)
    assert binding.inputs["BandHeight"] == round((bottom + 25 - (top - 25)) / 1080, 4)


def test_chapter_binding_is_the_centered_no_band_no_outline_card() -> None:
    # Given: the profile chapter style (97px on a full-black base)
    # When: resolving the chapter binding
    # Then: exactly the 7 reduced inputs — centered single line, outline
    #       off, band transparent (the black comes from the base video)
    binding = resolve_telop_binding(
        "chapter", "どこにもないカメラバッグ", load_default_profile().telop_style
    )
    assert dict(binding.inputs) == {
        "StyledText": "どこにもないカメラバッグ",
        "Font": "Hiragino Sans W6",
        "Size": round(97 / _TEMPLATE_SIZE_UNIT_PX, 4),
        "TextPos": [0.5, 0.5],
        "CharacterSpacing": 1.0,
        "OutlineEnabled": 0,
        "BandAlpha": 0.0,
    }
    assert binding.size == 0.0631


def test_template_size_unit_locks_the_measured_reference() -> None:
    # Given: the r4 size diagnosis — the template's Size input unit is a
    #        1536.7px reference measured from same-glyph probe cards
    #        (private/runtime/sol-size-diag-20260904/measurements-final.json
    #        k_calibration: k_mean 1536.7, intended 1080, ratio 1.4228);
    #        dividing px by 1080 rendered 1.42x too large and clipped both
    #        edges; Size 97/1536.7 = 0.0631-class matched the master
    # When: reading the divisor constant
    # Then: it equals the measured k_mean — a regression to 1080 here IS
    #       the too-large-text bug, and the profile px anchors map to the
    #       probe-proven fixed wire sizes
    assert _TEMPLATE_SIZE_UNIT_PX == 1536.7
    assert round(97 / _TEMPLATE_SIZE_UNIT_PX, 4) == 0.0631  # opening/chapter
    assert round(38 / _TEMPLATE_SIZE_UNIT_PX, 4) == 0.0247  # persistent
    assert round(4 / _TEMPLATE_SIZE_UNIT_PX, 4) == 0.0026  # opening outline


def test_unknown_font_refuses_typed_without_fallback() -> None:
    # Given: a profile font with no measurement font file mapping
    # When: resolving a fitted binding
    # Then: typed refusal — never a silent default font measurement
    style = load_default_profile().telop_style.model_copy(update={"font": "Fantasy Sans"})
    with pytest.raises(LiveAdapterUnsupportedError) as excinfo:
        resolve_telop_binding("persistent", EPISODE_TITLE, style)
    assert excinfo.value.code == "telop-font-file-unresolved"


def test_verify_telop_card_compares_bound_axes_and_normalizes_points() -> None:
    # Given: a binding and a matching independent readback
    binding = TelopCardBinding(
        styled_text="テロップ",
        font="Hiragino Sans W6",
        size=0.0352,
        text_pos=(0.1849, 0.0676),
        inputs={},
    )
    readback = TelopCardReadback(
        text="テロップ",
        font="Hiragino Sans W6",
        size=0.0352,
        text_pos={"1": 0.1849, "2": 0.0676, "3": 0.0},
    )
    # When: the verification runs
    # Then: it passes and returns the readback-sourced evidence
    assert verify_telop_card("card x", readback, binding) == {
        "font": "Hiragino Sans W6",
        "size": 0.0352,
        "text_pos": [0.1849, 0.0676],
    }


def test_verify_telop_card_drift_is_a_typed_failure_per_axis() -> None:
    # Given: a binding and readbacks drifted per axis (font, size, position)
    # When: the verification runs
    # Then: each drift is a typed telop-style-mismatch, never silent
    binding = TelopCardBinding(
        styled_text="x",
        font="Hiragino Sans W6",
        size=0.0352,
        text_pos=(0.1849, 0.0676),
        inputs={},
    )
    for readback in (
        TelopCardReadback("x", "Open Sans", 0.0352, {"1": 0.1849, "2": 0.0676}),
        TelopCardReadback("x", "Hiragino Sans W6", 0.09, {"1": 0.1849, "2": 0.0676}),
        TelopCardReadback("x", "Hiragino Sans W6", 0.0352, {"1": 0.5, "2": 0.5}),
        TelopCardReadback("x", "Hiragino Sans W6", 0.0352, {"2": 0.0676}),
    ):
        with pytest.raises(LiveAdapterError) as excinfo:
            verify_telop_card("card x", readback, binding)
        assert excinfo.value.code == "telop-style-mismatch"
