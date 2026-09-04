"""WBS-0 value locks + D style locks: the cue handler's style binding is
profile-sourced.

Before WBS-0, ``live_handlers/subtitle.py`` bound the single supported style
profile through an inline table (``_PROFILE_INPUTS``). WBS-0 moved the VALUES
into the channel presentation profile (DESIGN telop-nested §4.2); D (DESIGN
§5, 2026-09-04) updates them: size 0.055, one-weight-bolder font, drop shadow
ON (Text+ element 3), center unchanged. All wire names live-verified on the
D-sample disposable project 2026-09-04.
"""

from __future__ import annotations

import pytest

from services.creative_plan.presentation_intents import load_default_profile
from services.mcp_execution.live_handlers.common import LiveAdapterUnsupportedError
from services.mcp_execution.live_handlers.subtitle import _resolve_style_binding


def test_resolve_style_binding_locks_the_d_values() -> None:
    # Given: the one style profile id plans carry today ("subtitle-style-default")
    # When: resolving its Text+ binding through the channel presentation profile
    # Then: font/size/center/shadow equal the LIVE-VERIFIED D values (r3 probe
    #       2026-09-04, disposable __fvp_test__sol_d_sample_r3_20260904): W5
    #       resolves + renders (0.9198 PIL column-profile correlation, best
    #       match own weight); the shadow offset is the POINT input Offset3
    #       as [x, y] — the OffsetX3/OffsetY3 names are phantom successes
    #       (readback None)
    binding = _resolve_style_binding("subtitle-style-default")
    assert binding.font == "Hiragino Sans W5"
    assert binding.size == 0.055
    assert binding.center == (0.5, 0.14)
    assert binding.shadow_enabled == 1
    assert dict(binding.inputs) == {
        "Size": 0.055,
        "Center": [0.5, 0.14],
        "Enabled3": 1,
        "Red3": 0.0,
        "Green3": 0.0,
        "Blue3": 0.0,
        "Alpha3": 0.8,
        "Softness3": 0.005,
        "Offset3": [0.025, -0.04],
    }


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
    binding = _resolve_style_binding("subtitle-style-default")
    assert binding.font == "Hiragino Sans W6"
    assert binding.size == 0.05
    assert binding.center == (0.25, 0.75)
    # shadow rides the default block (the swap only changed font/size/center)
    assert binding.shadow_enabled == 1
    assert "Enabled3" in binding.inputs


def test_shadow_disabled_profile_binds_no_shadow_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a profile whose shadow block is disabled
    # When: resolving the binding
    # Then: no shadow wire inputs are written (the C silhouette)
    profile = load_default_profile()
    swapped = profile.model_copy(
        update={
            "subtitle_style": profile.subtitle_style.model_copy(
                update={
                    "shadow": profile.subtitle_style.shadow.model_copy(
                        update={"enabled": False}
                    )
                }
            )
        }
    )
    monkeypatch.setattr(
        "services.mcp_execution.live_handlers.subtitle.load_default_profile",
        lambda: swapped,
    )
    binding = _resolve_style_binding("subtitle-style-default")
    assert binding.shadow_enabled == 0
    assert binding.inputs == {"Size": 0.055, "Center": [0.5, 0.14]}
