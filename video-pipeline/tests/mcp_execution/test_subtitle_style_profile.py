"""WBS-0 value locks: the cue handler's style binding is profile-sourced.

Before WBS-0, ``live_handlers/subtitle.py`` bound the single supported style
profile through an inline table (``_PROFILE_INPUTS``). WBS-0 moves the VALUES
into the channel presentation profile (DESIGN telop-nested §4.2) with
byte-identical behavior: same accepted id, same typed refusal for any other
id, same font/size/center.
"""

from __future__ import annotations

import pytest

from services.creative_plan.presentation_intents import load_default_profile
from services.mcp_execution.live_handlers.common import LiveAdapterUnsupportedError
from services.mcp_execution.live_handlers.subtitle import _resolve_style_binding
from services.mcp_execution.live_handlers.subtitle_style import StyleBinding


def test_resolve_style_binding_equals_legacy_inline_constants():
    # Given: the one style profile id plans carry today ("subtitle-style-default")
    # When: resolving its Text+ binding through the channel presentation profile
    # Then: font/size/center are byte-identical to the pre-WBS-0 inline table
    assert _resolve_style_binding("subtitle-style-default") == StyleBinding(
        font="Hiragino Sans W3", size=0.04, center=(0.5, 0.14)
    )


def test_resolve_style_binding_unknown_id_refuses_typed():
    # Given: any style profile id outside the supported mapping
    # When: resolving its binding
    # Then: the same typed refusal as the inline-table era — never a default
    with pytest.raises(LiveAdapterUnsupportedError) as excinfo:
        _resolve_style_binding("prof-unknown")
    assert excinfo.value.code == "style-profile-unsupported"
    assert "no Text+ mapping for 'prof-unknown'" in str(excinfo.value)


def test_resolve_style_binding_reads_the_profile_not_a_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a channel profile whose subtitle appearance differs from default
    # When: resolving the supported style profile id
    # Then: the binding carries the profile's values — proving the profile is
    #       the source, not a coincidental literal
    profile = load_default_profile()
    swapped = profile.model_copy(
        update={
            "subtitle_style": profile.subtitle_style.model_copy(
                update={
                    "font": "Hiragino Sans W6",
                    "size_screen_ratio": 0.05,
                    "center": (0.25, 0.75),
                }
            )
        }
    )
    monkeypatch.setattr(
        "services.mcp_execution.live_handlers.subtitle.load_default_profile",
        lambda: swapped,
    )
    assert _resolve_style_binding("subtitle-style-default") == StyleBinding(
        font="Hiragino Sans W6", size=0.05, center=(0.25, 0.75)
    )
