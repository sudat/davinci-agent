"""Presentation intents + style-density guard tests (task 32).

Given the 10 PRD 10.2 semantic presentation intents and a Channel
Presentation Profile (PRD 10.6, all fields), the schema validates every kind
with table-driven per-kind params, and the guard:

(a) constructs + canonical-JSON round-trips all 10 kinds;
(b) rejects unknown kinds and params/kind mismatches (malformed input);
(c) accepts an intent set within every per-kind and global cap;
(d) rejects a per-kind over-limit with a typed error naming the cap + count;
(e) rejects a global per-minute over-limit;
(f) rejects punch-in params outside the profile range;
(g) exposes NO effect-introduction API (structural module-surface check —
    the Gate-quota prohibition is enforced by the module surface itself);
(h) loads the shipped default profile and round-trips it (stale state).

Task-31 seam: attach_presentation_intents returns a new TimelineIrV2 with
presentation_intent_refs set; the source IR stays untouched.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import FunctionType
from typing import get_args

import pytest
from pydantic import ValidationError

import services.creative_plan.presentation_intents as pi
from services.creative_plan.ir_models_v2 import TimelineIrV2
from services.creative_plan.presentation_intents import (
    ALL_KINDS,
    AudioPolicy,
    BrandAssets,
    ChannelPresentationProfile,
    ClosedRange,
    ColorPolicy,
    DensityLimitExceededError,
    DensityLimits,
    DensityReport,
    IntroOutroRules,
    PresentationIntentKind,
    PresentationIntentV2,
    ProfileRangeViolationError,
    PunchInRange,
    SfxPolicy,
    SubtitleShadowStyle,
    SubtitleStyle,
    TelopBand,
    TelopChapterStyle,
    TelopOpeningBox,
    TelopOpeningStyle,
    TelopOutline,
    TelopPersistentBox,
    TelopPersistentSecondStyle,
    TelopPersistentStyle,
    TelopStyle,
    TitleChoices,
    TransitionPreferences,
    attach_presentation_intents,
    check_density,
    load_default_profile,
    validate_against_profile,
)
from services.policy.check_scope import scan_config_tree
from services.presentation import chapter_card
from services.presentation import theme_text as tt

IR_JSON = """
{
  "schema_version": "timeline-ir-v2",
  "episode_id": "ep-t",
  "rate": {"num": 30, "den": 1},
  "video_tracks": [{
    "role": "primary",
    "track_id": "vt-1",
    "items": [{
      "item_id": "clip-1",
      "source": {"source_id": "src-1",
                 "span": {"start_frame": 0, "end_frame": 90,
                          "rate": {"num": 30, "den": 1}}},
      "record_span": {"start_frame": 0, "end_frame": 90},
      "candidate_ref": "cand-1"
    }]
  }]
}
"""

_VALID_PARAMS: dict[str, dict[str, object]] = {
    "emphasis_punch_in": {"scale": 1.15, "duration_frames": 24},
    "broll_cutaway": {"content_hint": "city skyline"},
    "lower_third": {"text": "Guest: Ada L.", "duration_frames": 120},
    "keyword_text": {"text": "30-minute rule", "duration_frames": 36},
    "chapter_card": {"title": "Chapter 1", "duration_frames": 90},
    "simple_dissolve": {"duration_frames": 12},
    "motion_transition": {"duration_frames": 18},
    "picture_in_picture": {"scale": 0.3, "corner": "bottom_right"},
    "screen_highlight": {
        "center_x": 0.5,
        "center_y": 0.5,
        "width": 0.4,
        "height": 0.3,
    },
    "sfx_accent": {"cue_hint": "whoosh"},
}

EXPECTED_SURFACE = {
    "attach_presentation_intents",
    "check_density",
    "load_default_profile",
    "validate_against_profile",
}


def _intent(
    kind: str,
    params: dict[str, object] | None = None,
    *,
    ordinal: int = 0,
    start: int = 0,
) -> PresentationIntentV2:
    payload = {
        "intent_id": f"pi-{kind}-{ordinal}",
        "kind": kind,
        "target_span": {"start_frame": start, "end_frame": start + 24},
        "params": _VALID_PARAMS[kind] if params is None else params,
        "rationale": "test intent: emphasis for pacing",
    }
    return PresentationIntentV2.model_validate(payload)


def _profile(*, per_kind_cap: float = 3.0, global_per_minute: float = 12.0):
    return ChannelPresentationProfile(
        schema_version="channel-presentation-profile-v1",
        channel_id="ch-test",
        allowed_recipe_families=(
            "audio",
            "branding",
            "color",
            "motion",
            "subtitle",
            "title",
            "transitions",
        ),
        density=DensityLimits(
            per_kind=dict.fromkeys(ALL_KINDS, per_kind_cap),
            global_per_minute=global_per_minute,
        ),
        title_choices=TitleChoices(
            opening="title/opening",
            chapter="title/opening",
            lower_third="title/lower-third",
        ),
        subtitle_style=SubtitleStyle(
            recipe_id="subtitle/default",
            max_line_length_chars=32,
            font_size_px=24,
            font="Hiragino Sans W3",
            size_screen_ratio=0.04,
            center=(0.5, 0.14),
            shadow=SubtitleShadowStyle(
                enabled=False,
                color=(0, 0, 0),
                alpha=204,
                softness=0.005,
                offset_x=0.004,
                offset_y=-0.004,
            ),
        ),
        telop_style=TelopStyle(
            recipe_id="telop/default",
            font="Hiragino Sans W6",
            opening=TelopOpeningStyle(
                size_px_base=97,
                size_px_min=64,
                box=TelopOpeningBox(x=96, w=1632, center_v=True),
                band=TelopBand(fill=(10, 10, 10), alpha=140, pad_x=23, pad_y=19),
                outline=TelopOutline(color=(16, 16, 16), width_ratio=0.04, min_px=2),
            ),
            persistent=TelopPersistentStyle(
                size_px_base=43,
                size_px_min=27,
                box=TelopPersistentBox(x=48, y=27, w=624),
                band=TelopBand(fill=(10, 10, 10), alpha=140, pad_x=15, pad_y=12),
                outline=TelopOutline(color=(16, 16, 16), width_ratio=0.04, min_px=2),
                text_color=(255, 255, 255),
            ),
            persistent_second=TelopPersistentSecondStyle(
                size_px_base=30,
                size_px_min=20,
                band=TelopBand(fill=(10, 10, 10), alpha=140, pad_x=15, pad_y=10),
                text_color=(255, 255, 255),
                indent_right_px=24,
                offset_below_px=0,
            ),
            chapter=TelopChapterStyle(size_px=97, background="black-full"),
        ),
        transition_preferences=TransitionPreferences(
            preferred_order=("dissolve", "motion", "hard_cut"), max_duration_frames=30
        ),
        punch_in_range=PunchInRange(min=1.05, max=1.3),
        audio_policy=AudioPolicy(
            dialogue_lufs=ClosedRange(min=-23.0, max=-14.0),
            true_peak_db=ClosedRange(min=-3.0, max=-1.0),
            bgm_duck_level_db=ClosedRange(min=-20.0, max=-6.0),
            sfx=SfxPolicy(allowed=True, max_per_minute=3.0),
        ),
        color_policy=ColorPolicy(
            technical_normalize_required=True,
            shot_match_required=True,
            channel_look="color/channel-look",
        ),
        intro_outro=IntroOutroRules(mode="optional", max_duration_frames=180),
        brand_assets=BrandAssets(
            fonts=("NotoSansJP",), logo=None, safe_margin_pct=5.0, licenses=()
        ),
    )


# ------------------------------------------------------ kinds + round-trip


def test_kind_table_matches_literal_exactly_ten():
    assert set(ALL_KINDS) == set(get_args(PresentationIntentKind))
    assert len(ALL_KINDS) == 10


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_every_kind_constructs_and_round_trips(kind):
    intent = _intent(kind)
    assert intent.kind == kind
    parsed = PresentationIntentV2.model_validate_json(intent.model_dump_json())
    assert parsed == intent
    assert parsed.params == intent.params


# ------------------------------------------------------ malformed input


def test_unknown_kind_rejected():
    payload = {
        "intent_id": "pi-bad",
        "kind": "wipe_transition",
        "target_span": {"start_frame": 0, "end_frame": 12},
        "params": {},
        "rationale": "never a valid semantic kind",
    }
    with pytest.raises(ValidationError, match="wipe_transition"):
        PresentationIntentV2.model_validate(payload)


def test_params_kind_mismatch_rejected():
    payload = {
        "intent_id": "pi-mix",
        "kind": "chapter_card",
        "target_span": {"start_frame": 0, "end_frame": 60},
        "params": {"scale": 1.2, "duration_frames": 10},
        "rationale": "punch-in params on a chapter card",
    }
    with pytest.raises(ValidationError):
        PresentationIntentV2.model_validate(payload)


# ------------------------------------------------------ within limits


def test_within_limits_intent_set_passes():
    profile = _profile()
    intents = [_intent(kind, ordinal=0) for kind in ALL_KINDS]
    report = check_density(intents, profile, timeline_duration_seconds=600.0)
    assert isinstance(report, DensityReport)
    assert report.intent_count == 10
    assert report.violations == ()
    assert report.counts_by_kind["chapter_card"] == 1


# ------------------------------------------------------ density caps


def test_per_kind_over_limit_names_cap_and_count():
    profile = _profile(per_kind_cap=3.0, global_per_minute=100.0)
    intents = [_intent("sfx_accent", ordinal=i) for i in range(4)]
    with pytest.raises(DensityLimitExceededError) as excinfo:
        check_density(intents, profile, timeline_duration_seconds=60.0)
    (violation,) = excinfo.value.report.violations
    assert violation.scope == "per_kind"
    assert violation.kind == "sfx_accent"
    assert violation.count == 4
    assert "sfx_accent" in str(excinfo.value)
    assert "3.00/min" in str(excinfo.value)


def test_global_over_limit_rejected():
    profile = _profile(per_kind_cap=50.0, global_per_minute=5.0)
    intents = [_intent("sfx_accent", ordinal=i) for i in range(6)]
    with pytest.raises(DensityLimitExceededError) as excinfo:
        check_density(intents, profile, timeline_duration_seconds=60.0)
    (violation,) = excinfo.value.report.violations
    assert violation.scope == "global"
    assert violation.kind is None
    assert "global" in str(excinfo.value)


def test_non_positive_duration_rejected():
    with pytest.raises(ValueError, match="positive"):
        check_density([_intent("sfx_accent")], _profile(), timeline_duration_seconds=0.0)


# ------------------------------------------------ punch-in profile range


def test_punch_in_out_of_profile_range_rejected():
    profile = _profile()
    hot = _intent("emphasis_punch_in", {"scale": 1.6, "duration_frames": 24})
    with pytest.raises(ProfileRangeViolationError, match="punch_in"):
        validate_against_profile([hot], profile)


def test_punch_in_within_profile_range_accepted():
    profile = _profile()
    ok = _intent("emphasis_punch_in", {"scale": 1.2, "duration_frames": 24})
    assert validate_against_profile([ok], profile) is None


# ------------------------------------------- no effect-introduction API


def test_no_effect_introduction_api():
    own_functions = {
        name
        for name, member in vars(pi).items()
        if isinstance(member, FunctionType)
        and not name.startswith("_")
        and member.__module__ == pi.__name__
    }
    assert own_functions == EXPECTED_SURFACE
    assert not [
        name
        for name in own_functions
        if re.search(r"(?i)insert|append|ensure|satisfy|spawn|create", name)
    ]
    for model in (pi.PresentationIntentV2, pi.ChannelPresentationProfile, pi.DensityReport):
        assert not [
            m for m in vars(model) if re.search(r"(?i)insert|append|ensure|satisfy|spawn", m)
        ]


# ------------------------------------------------------ default profile


def test_default_profile_loads_and_round_trips():
    profile = load_default_profile()
    assert profile.channel_id == "default"
    assert profile.density.global_per_minute > 0
    assert all(profile.density.per_kind[kind] > 0 for kind in ALL_KINDS)
    assert profile.punch_in_range.min >= 1.0
    again = ChannelPresentationProfile.model_validate_json(profile.model_dump_json())
    assert again == profile


def test_default_profile_config_keys_pass_scope_guard():
    # Kind names (e.g. motion_transition) must be wire VALUES, never JSON
    # keys — config keys carrying phase-4 tokens break check_scope --config.
    violations = scan_config_tree(Path(__file__).resolve().parents[2])
    assert violations == []


def test_density_wire_entry_duplicate_kind_rejected():
    payload = {
        "per_kind": [
            {"kind": "sfx_accent", "cap_per_minute": 1.0},
            {"kind": "sfx_accent", "cap_per_minute": 2.0},
        ],
        "global_per_minute": 5.0,
    }
    with pytest.raises(ValidationError, match="twice"):
        DensityLimits.model_validate(payload)


def test_density_missing_kind_cap_rejected():
    payload = {
        "per_kind": [{"kind": "sfx_accent", "cap_per_minute": 1.0}],
        "global_per_minute": 5.0,
    }
    with pytest.raises(ValidationError, match="missing"):
        DensityLimits.model_validate(payload)


# ---------------------------------------------------- task-31 IR seam


def test_attach_presentation_intents_sets_refs_additively():
    ir = TimelineIrV2.model_validate_json(IR_JSON)
    intents = [
        _intent("lower_third", ordinal=0),
        _intent("keyword_text", ordinal=1),
    ]
    attached = attach_presentation_intents(ir, intents)
    assert attached.presentation_intent_refs == ("pi-lower_third-0", "pi-keyword_text-1")
    assert ir.presentation_intent_refs == ()
    assert attached.episode_id == ir.episode_id


def test_attach_rejects_duplicate_intent_ids():
    ir = TimelineIrV2.model_validate_json(IR_JSON)
    first = _intent("sfx_accent", ordinal=0)
    second = _intent("sfx_accent", ordinal=0)
    with pytest.raises(ValueError, match="unique"):
        attach_presentation_intents(ir, [first, second])


# ------------------------------- WBS-0 telop/subtitle style externalized values
#
# The profile is now the style container (DESIGN telop-nested §4.2), but the
# calculator stays theme_text.py / chapter_card.py. These locks pin the
# profile fields byte-identical to those code constants so moving the values
# can never drift from the renderer that still consumes them.


def test_default_profile_subtitle_style_locks_live_card_appearance():
    # Given: the shipped default profile
    # When: reading the subtitle appearance fields
    # Then: the D values (DESIGN telop-nested §5, r3 revision) — center
    #       unchanged, size 0.055, shadow on, font one weight bolder (W4 →
    #       W5, LIVE-VERIFIED on the r3 disposable probe 2026-09-04); the
    #       offset is the measured ≈6px-down-right class.
    style = load_default_profile().subtitle_style
    assert style.font == "Hiragino Sans W5"
    assert style.size_screen_ratio == 0.055
    assert style.center == (0.5, 0.14)
    assert style.shadow.enabled is True
    assert style.shadow.color == (0, 0, 0)
    assert style.shadow.alpha == 204
    assert style.shadow.softness == 0.005
    assert style.shadow.offset_x == 0.025
    assert style.shadow.offset_y == -0.04


def test_default_profile_telop_style_opening_mirrors_theme_text():
    # Given: the shipped default profile and theme_text.py's opening box
    # When: comparing every opening telop field
    # Then: sizes/box/band match the constants exactly; outline matches the
    #       render-time derivation (half of _stroke_for's 8% ratio, min 2 —
    #       theme_text.py render_theme_text: stroke = max(2, stroke_width//2),
    #       stroke_fill=(16,16,16,255))
    opening = load_default_profile().telop_style.opening
    assert opening.size_px_base == tt._OPEN_BASE == 97
    assert opening.size_px_min == tt._OPEN_MIN == 64
    assert opening.box.x == tt._OPEN_X == 96
    assert opening.box.w == tt._OPEN_W == 1632
    assert opening.box.center_v is True
    assert opening.band.fill == tt._BAND_FILL[:3] == (10, 10, 10)
    assert opening.band.alpha == tt._BAND_FILL[3] == 140
    assert opening.band.pad_x == tt._BAND_PADDING["opening"][0] == 23
    assert opening.band.pad_y == tt._BAND_PADDING["opening"][1] == 19
    assert opening.outline.color == (16, 16, 16)
    assert opening.outline.width_ratio == 0.04
    assert opening.outline.min_px == 2


def test_default_profile_telop_style_persistent_mirrors_theme_text():
    # Given: the shipped default profile and theme_text.py's persistent box
    # When: comparing every persistent telop field
    # Then: layout/sizes/box still mirror the calculator constants exactly
    #       (the fit geometry is D-unchanged), while the D colors are
    #       profile-owned: white band at the adjusted opacity + dark text
    #       (the deliberate D divergence from the C-era calculator mirror)
    persistent = load_default_profile().telop_style.persistent
    assert persistent.size_px_base == tt._PERS_BASE == 43
    assert persistent.size_px_min == tt._PERS_MIN == 27
    assert persistent.box.x == tt._PERS_X == 48
    assert persistent.box.y == tt._PERS_Y == 27
    assert persistent.box.w == tt._PERS_W == 624
    assert persistent.band.pad_x == tt._BAND_PADDING["persistent"][0] == 15
    assert persistent.band.pad_y == tt._BAND_PADDING["persistent"][1] == 12
    assert persistent.band.fill == (255, 255, 255)
    assert persistent.band.alpha == 230
    assert persistent.text_color == (10, 10, 10)
    assert persistent.outline.color == (16, 16, 16)
    assert persistent.outline.width_ratio == 0.04
    assert persistent.outline.min_px == 2


def test_default_profile_telop_style_persistent_second_locks_d_block():
    # Given: the shipped default profile
    # When: reading the persistent_second block
    # Then: the D values — fully-opaque dark band (alpha 255: the D-sample
    #       dark-footage measurement showed alpha 140 leaves a 6.0 same-site
    #       luma delta, below the ~10 readability bar — sol-d-sample-r2
    #       cause report §6), white glyphs, right indent 24px, offset 0
    #       (flush below the L1 band, DESIGN D 注2) — gate-tunable once,
    #       never silently defaulted
    second = load_default_profile().telop_style.persistent_second
    assert second.size_px_base == 36
    assert second.size_px_min == 20
    assert second.band.fill == (10, 10, 10)
    assert second.band.alpha == 255
    assert second.band.pad_x == 15
    assert second.band.pad_y == 10
    assert second.text_color == (255, 255, 255)
    assert second.indent_right_px == 24
    assert second.offset_below_px == 0


def test_channel_profile_without_persistent_second_refused_no_silent_default():
    # Given: the shipped default payload minus the persistent_second block
    # When: validating as a channel presentation profile
    # Then: strict rejection — the D second layer is required, not defaulted
    payload = json.loads(pi._DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))
    del payload["telop_style"]["persistent_second"]
    with pytest.raises(ValidationError, match="persistent_second"):
        ChannelPresentationProfile.model_validate(payload)


def test_channel_profile_without_subtitle_shadow_refused():
    # Given: the shipped default payload minus the shadow block
    # When: validating as a channel presentation profile
    # Then: strict rejection — the shadow container never silently drops
    payload = json.loads(pi._DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))
    del payload["subtitle_style"]["shadow"]
    with pytest.raises(ValidationError, match="shadow"):
        ChannelPresentationProfile.model_validate(payload)


def test_default_profile_telop_style_chapter_mirrors_chapter_card():
    # Given: the shipped default profile and chapter_card.py's approved card
    # When: comparing the chapter telop fields
    # Then: size equals FONT_SIZE_PX and the background stays the pure black
    #       full canvas (chapter_card renders Image.new "RGB" fill (0,0,0))
    chapter = load_default_profile().telop_style.chapter
    assert chapter.size_px == chapter_card.FONT_SIZE_PX == 97
    assert chapter.background == "black-full"


def test_default_profile_telop_style_identity_fields():
    telop = load_default_profile().telop_style
    assert telop.recipe_id == "telop/default"
    # DESIGN §3/§7: bind a concrete resolvable weight name, never a bare
    # family name (subtitle readback lesson).
    assert telop.font == "Hiragino Sans W6"


def test_default_profile_style_fields_round_trip_through_json():
    # Given: the shipped default profile (nested tuples in center/fill/color)
    # When: serializing to JSON and re-validating
    # Then: every telop/subtitle style field survives byte-identically
    profile = load_default_profile()
    again = ChannelPresentationProfile.model_validate_json(profile.model_dump_json())
    assert again == profile
    assert again.telop_style == profile.telop_style
    assert again.subtitle_style == profile.subtitle_style


def test_channel_profile_without_telop_style_refused_no_silent_default():
    # Given: the shipped default payload minus the telop_style block
    # When: validating as a channel presentation profile
    # Then: strict rejection — a missing style container never falls back
    payload = json.loads(pi._DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))
    del payload["telop_style"]
    with pytest.raises(ValidationError, match="telop_style"):
        ChannelPresentationProfile.model_validate(payload)


def test_channel_profile_without_subtitle_appearance_refused():
    # Given: the shipped default payload minus one subtitle appearance field
    # When: validating as a channel presentation profile
    # Then: strict rejection — appearance values are required, not defaulted
    payload = json.loads(pi._DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))
    del payload["subtitle_style"]["size_screen_ratio"]
    with pytest.raises(ValidationError, match="size_screen_ratio"):
        ChannelPresentationProfile.model_validate(payload)


def test_telop_style_rejects_inverted_size_ladder_and_unknown_background():
    # Given: a valid telop_style payload
    # When: inverting the size ladder, or naming an unknown chapter background
    # Then: typed validation rejections (no silent clamping)
    payload = json.loads(pi._DEFAULT_PROFILE_PATH.read_text())["telop_style"]
    inverted = {**payload, "opening": {**payload["opening"], "size_px_min": 999}}
    with pytest.raises(ValidationError, match="size_px_min"):
        pi.TelopStyle.model_validate(inverted)
    unknown_bg = {**payload, "chapter": {**payload["chapter"], "background": "white"}}
    with pytest.raises(ValidationError, match=r"black-full|background"):
        pi.TelopStyle.model_validate(unknown_bg)
