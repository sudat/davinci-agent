"""Structural invariants of the tracked telop template asset.

The WBS-4 C-gate round-1 bisect (private/runtime/sol-c-variant-gate-20260903,
``white_render_bisect``) isolated the first white-render defect to the
template's own output wiring: the group's MainOutput was the KeyStretcher,
a matte-stretching tool that slams near-zero alpha opaque across the whole
frame — so every placed card whitened the composite even with the telop
track disabled and BandAlpha=0. The fix (asset revision 2) rewires the
MainOutput to the band Merge and drops the leftover KeyStretcher node.

These tests lock that wiring by parsing the ``.setting`` text directly
(Fusion's Lua-table settings format — data, never executed), so a wiring
regression fails here instead of at the live render gate.
"""

from __future__ import annotations

import re
from typing import Final

from services.mcp_execution.live_handlers.telop_card import TELOP_TEMPLATE_ASSET

#: Inner-tool types this template may legitimately contain (the outer
#: ``Template = GroupOperator`` wrapper is asserted separately). Anything
#: else (KeyStretcher included) must be a conscious asset revision + sha
#: re-pin.
_TOOL_VOCABULARY: Final = frozenset(
    {"RectangleMask", "Background", "TextPlus", "Merge"}
)

_TOOL_DEFS: Final[re.Pattern[str]] = re.compile(
    r"^\t+(\w+) = (\w+) \{$", re.MULTILINE
)
_MAIN_OUTPUT: Final[re.Pattern[str]] = re.compile(
    r"MainOutput1 = InstanceOutput \{\s*"
    r'SourceOp = "([^"]+)",\s*'
    r'Source = "([^"]+)",',
    re.DOTALL,
)
_PUBLISHED_INPUT: Final[re.Pattern[str]] = re.compile(
    r"(\w+) = InstanceInput \{\s*SourceOp = \"(\w+)\""
)
_CONNECTION: Final[re.Pattern[str]] = re.compile(
    r"(\w+) = Input \{\s*SourceOp = \"(\w+)\",\s*Source = \"(\w+)\""
)

#: The published-input contract the product code writes (WBS-1 probe
#: ``wbs2_payload.published_input_keys``; ``Style`` is the one name the
#: live round-trip did not exercise, but the asset must still publish it).
_PUBLISHED_INPUT_NAMES: Final = frozenset(
    {
        "StyledText",
        "Font",
        "Style",
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
)


def _setting_text() -> str:
    return TELOP_TEMPLATE_ASSET.read_text(encoding="utf-8")


def _inner_tools(text: str) -> dict[str, str]:
    """Tool name → tool type for known Fusion tool definitions only."""
    return {
        name: tool_type
        for name, tool_type in _TOOL_DEFS.findall(text)
        if tool_type in _TOOL_VOCABULARY
    }


def _connections(text: str) -> set[tuple[str, str, str]]:
    """Every ``<input> = Input { SourceOp, Source }`` edge as (input, op, source)."""
    return set(_CONNECTION.findall(text))


def test_main_output_is_the_band_merge() -> None:
    # Given: the tracked template asset (revision 2 wiring)
    # When: parsing the group's MainOutput definition
    # Then: exactly one MainOutput, sourced from the Merge that composites
    #       band + text — never from a matte tool
    outputs = _MAIN_OUTPUT.findall(_setting_text())
    assert len(outputs) == 1
    source_op, source = outputs[0]
    assert (source_op, source) == ("TextOverBand", "Output")
    assert _inner_tools(_setting_text())["TextOverBand"] == "Merge"


def test_no_matte_tool_anywhere_in_the_asset() -> None:
    # Given: the white-render bisect verdict (KeyStretcher as MainOutput)
    # When: parsing every tool definition in the asset
    # Then: no KeyStretcher survives anywhere — it was a pure mis-wired
    #       leftover (sole input was the finished composite; nothing in
    #       the band/matte chain consumed it) — and the inner tools are
    #       exactly the four-node band graph in file order
    text = _setting_text()
    assert "KeyStretcher" not in text
    all_defs = _TOOL_DEFS.findall(text)
    assert ("Template", "GroupOperator") in all_defs
    assert [pair for pair in all_defs if pair[1] in _TOOL_VOCABULARY] == [
        ("BandMask", "RectangleMask"),
        ("Band", "Background"),
        ("Text", "TextPlus"),
        ("TextOverBand", "Merge"),
    ]
    assert set(_inner_tools(text)) == {"BandMask", "Band", "Text", "TextOverBand"}


def test_band_chain_feeds_the_output() -> None:
    # Given: the intended band telop graph
    # When: parsing the asset's connections
    # Then: mask→band→merge→output is wired exactly as designed and the
    #       merge is the only node the MainOutput can draw from
    edges = _connections(_setting_text())
    assert ("EffectMask", "BandMask", "Mask") in edges
    assert ("Background", "Band", "Output") in edges
    assert ("Foreground", "Text", "Output") in edges
    assert ("Keyframes", "TextOverBand", "Output") not in edges


def test_published_input_contract_is_untouched() -> None:
    # Given: telop_card.py writes these exact input names via safe_set_inputs
    # When: parsing the group's InstanceInput block
    # Then: the published name set is exactly the probe-verified contract
    #       (20 names; 19 live round-tripped in WBS-1 check-c) and every
    #       input still binds to a surviving inner tool
    published = dict(_PUBLISHED_INPUT.findall(_setting_text()))
    assert frozenset(published) == _PUBLISHED_INPUT_NAMES
    assert set(published.values()) <= {"Text", "Band", "BandMask"}
