"""Semantic presentation intents + style-density guard (task 32; PRD 10.2/10.6).

The 10 PRD 10.2 semantic intents stay NLE-neutral (ResolveFreeModel): typed
per-kind params are TABLE-DRIVEN (``_PARAM_MODELS``) and validated against
the intent's ``kind`` at the model. Recipe selection/execution is tasks
36/37; IR compilation is task 38 — this module only describes and validates
intent.

``ChannelPresentationProfile`` models EVERY PRD 10.6 field (allowed recipe
families + density caps per-kind/global, title/lower-third choices, subtitle
style, transition preferences, punch-in range, audio policy + target ranges,
color policy, intro/outro rules, brand assets + license evidence). The
shipped default (config/production-kit/presentation-profile-default.json)
carries conservative caps; no v4.2 channel profile existed to extend, and
services/config loaders are untouched.

Gate-quota prohibition (PRD 10.2), enforced STRUCTURALLY: the density guard
only validates or rejects — the module's entire function surface is
check_density / validate_against_profile / attach_presentation_intents /
load_default_profile, and a test freezes that surface so no path that
introduces an effect to satisfy a Gate can appear unnoticed.

Task-31 seam: attach_presentation_intents returns a NEW TimelineIrV2 with
presentation_intent_refs set (ids must be unique); T31 modules stay frozen.
"""

# allow: SIZE_OK — pure schema + guard definitions mirroring ir_models_v2.py;
# splitting would separate the per-kind param table from the intent model
# that guards it. Task-31 single-module precedent.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, get_args

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    SerializeAsAny,
    model_serializer,
    model_validator,
)
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Identifier,
    RecordFrameSpan,
    ResolveFreeModel,
    StrictModel,
    to_tuple,
)

if TYPE_CHECKING:
    from services.creative_plan.ir_models_v2 import TimelineIrV2

PresentationIntentKind = Literal[
    "emphasis_punch_in",
    "broll_cutaway",
    "lower_third",
    "keyword_text",
    "chapter_card",
    "simple_dissolve",
    "motion_transition",
    "picture_in_picture",
    "screen_highlight",
    "sfx_accent",
]

ALL_KINDS: tuple[PresentationIntentKind, ...] = (
    "emphasis_punch_in",
    "broll_cutaway",
    "lower_third",
    "keyword_text",
    "chapter_card",
    "simple_dissolve",
    "motion_transition",
    "picture_in_picture",
    "screen_highlight",
    "sfx_accent",
)

_Frames = Annotated[int, Field(gt=0, strict=True)]
_NonEmpty = Annotated[str, Field(min_length=1, strict=True)]
RecipeRef = _NonEmpty


# ------------------------------------------------------------ per-kind params


class PunchInParams(StrictModel):
    scale: float = Field(gt=1.0, le=2.0, strict=True)
    duration_frames: _Frames


class CutawayParams(StrictModel):
    content_hint: str = ""


class LowerThirdParams(StrictModel):
    text: _NonEmpty
    duration_frames: _Frames


class KeywordTextParams(StrictModel):
    text: _NonEmpty
    duration_frames: _Frames


class ChapterCardParams(StrictModel):
    title: _NonEmpty
    duration_frames: _Frames


class SimpleDissolveParams(StrictModel):
    duration_frames: _Frames


class MotionTransitionParams(StrictModel):
    duration_frames: _Frames


class PictureInPictureParams(StrictModel):
    scale: float = Field(gt=0.0, lt=1.0, strict=True)
    corner: Literal["top_left", "top_right", "bottom_left", "bottom_right"]


class ScreenHighlightParams(StrictModel):
    center_x: float = Field(ge=0.0, le=1.0, strict=True)
    center_y: float = Field(ge=0.0, le=1.0, strict=True)
    width: float = Field(gt=0.0, le=1.0, strict=True)
    height: float = Field(gt=0.0, le=1.0, strict=True)


class SfxAccentParams(StrictModel):
    cue_hint: str = ""


_PARAM_MODELS: Mapping[str, type[StrictModel]] = {
    "emphasis_punch_in": PunchInParams,
    "broll_cutaway": CutawayParams,
    "lower_third": LowerThirdParams,
    "keyword_text": KeywordTextParams,
    "chapter_card": ChapterCardParams,
    "simple_dissolve": SimpleDissolveParams,
    "motion_transition": MotionTransitionParams,
    "picture_in_picture": PictureInPictureParams,
    "screen_highlight": ScreenHighlightParams,
    "sfx_accent": SfxAccentParams,
}


class PresentationIntentV2(ResolveFreeModel):
    """One semantic presentation intent over a record-frame span (PRD 10.2)."""

    intent_id: Identifier
    kind: PresentationIntentKind
    target_span: RecordFrameSpan
    params: SerializeAsAny[StrictModel]
    rationale: _NonEmpty

    @model_validator(mode="before")
    @classmethod
    def _type_params_by_kind(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        kind = value.get("kind")
        model = _PARAM_MODELS.get(kind) if isinstance(kind, str) else None
        if model is None or not isinstance(value.get("params"), dict):
            return value
        return {**value, "params": model.model_validate(value["params"])}

    @model_validator(mode="after")
    def require_params_match_kind(self) -> PresentationIntentV2:
        if not isinstance(self.params, _PARAM_MODELS[self.kind]):
            raise PydanticCustomError(
                "params_kind_mismatch",
                "params do not match the {kind} intent kind",
                {"kind": self.kind},
            )
        return self


# ------------------------------------------- Channel Presentation Profile 10.6


class ClosedRange(StrictModel):
    """Inclusive min/max pair."""

    min: float = Field(strict=True)
    max: float = Field(strict=True)

    @model_validator(mode="after")
    def require_ordered(self) -> ClosedRange:
        if self.max < self.min:
            raise PydanticCustomError("range_inverted", "max must be >= min")
        return self

    def covers(self, value: float) -> bool:
        return self.min <= value <= self.max


class PunchInRange(ClosedRange):
    """Punch-in scale bounds; a punch-in never zooms out (min >= 1.0)."""

    @model_validator(mode="after")
    def require_zoom_in(self) -> PunchInRange:
        if self.min < 1.0:
            raise PydanticCustomError("punch_in_below_unity", "punch_in_range.min must be >= 1.0")
        return self


class DensityCapEntry(StrictModel):
    """Wire form of one per-kind cap (kinds are VALUES, never JSON keys —
    the phase-3 scope guard forbids config keys carrying tokens like
    ``motion``; PRD 10.2 kind names stay untouched as values)."""

    kind: PresentationIntentKind
    cap_per_minute: float = Field(ge=0.0, strict=True)


class DensityLimits(StrictModel):
    per_kind: dict[PresentationIntentKind, float] = Field(min_length=1)
    global_per_minute: float = Field(ge=0.0, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _parse_wire_entries(cls, value: object) -> object:
        if isinstance(value, dict):
            per_kind = value.get("per_kind")
            if isinstance(per_kind, list):
                mapped: dict[str, float] = {}
                for entry in per_kind:
                    parsed = DensityCapEntry.model_validate(entry)
                    if parsed.kind in mapped:
                        raise PydanticCustomError(
                            "duplicate_density_cap",
                            "density cap for {kind} is declared twice",
                            {"kind": parsed.kind},
                        )
                    mapped[parsed.kind] = parsed.cap_per_minute
                value = {**value, "per_kind": mapped}
            elif isinstance(per_kind, dict):
                value = {**value, "per_kind": dict(sorted(per_kind.items()))}
        return value

    @model_validator(mode="after")
    def require_every_kind_capped(self) -> DensityLimits:
        missing = [kind for kind in get_args(PresentationIntentKind) if kind not in self.per_kind]
        if missing:
            raise PydanticCustomError(
                "missing_density_cap",
                "density caps must cover every intent kind; missing: {missing}",
                {"missing": ", ".join(missing)},
            )
        return self

    @model_serializer
    def _serialize_wire_entries(self) -> dict[str, object]:
        return {
            "per_kind": [
                {"kind": kind, "cap_per_minute": self.per_kind[kind]}
                for kind in ALL_KINDS
                if kind in self.per_kind
            ],
            "global_per_minute": self.global_per_minute,
        }


class TitleChoices(StrictModel):
    opening: RecipeRef
    chapter: RecipeRef
    lower_third: RecipeRef


class SubtitleStyle(StrictModel):
    recipe_id: RecipeRef
    max_line_length_chars: int = Field(gt=0, strict=True)
    font_size_px: int = Field(gt=0, strict=True)


class TransitionPreferences(StrictModel):
    preferred_order: Annotated[
        tuple[Literal["dissolve", "motion", "hard_cut"], ...], BeforeValidator(to_tuple)
    ] = Field(min_length=1)
    max_duration_frames: _Frames


class SfxPolicy(StrictModel):
    allowed: bool
    max_per_minute: float = Field(ge=0.0, strict=True)


class AudioPolicy(StrictModel):
    dialogue_lufs: ClosedRange
    true_peak_db: ClosedRange
    bgm_duck_level_db: ClosedRange
    sfx: SfxPolicy


class ColorPolicy(StrictModel):
    technical_normalize_required: bool
    shot_match_required: bool
    channel_look: RecipeRef


class IntroOutroRules(StrictModel):
    mode: Literal["required", "optional", "none"]
    max_duration_frames: _Frames


class LicenseEvidence(StrictModel):
    asset: _NonEmpty
    license: _NonEmpty
    evidence: _NonEmpty


class BrandAssets(StrictModel):
    fonts: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = Field(min_length=1)
    logo: str | None = None
    safe_margin_pct: float = Field(ge=0.0, le=50.0, strict=True)
    licenses: Annotated[tuple[LicenseEvidence, ...], BeforeValidator(to_tuple)] = ()


def _canonical_families(families: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(families)))


class ChannelPresentationProfile(ResolveFreeModel):
    """PRD 10.6 Channel Presentation Profile — policy over the Production Kit."""

    schema_version: Literal["channel-presentation-profile-v1"]
    channel_id: Identifier
    allowed_recipe_families: Annotated[
        tuple[RecipeRef, ...], BeforeValidator(to_tuple), AfterValidator(_canonical_families)
    ] = Field(min_length=1)
    density: DensityLimits
    title_choices: TitleChoices
    subtitle_style: SubtitleStyle
    transition_preferences: TransitionPreferences
    punch_in_range: PunchInRange
    audio_policy: AudioPolicy
    color_policy: ColorPolicy
    intro_outro: IntroOutroRules
    brand_assets: BrandAssets


# ------------------------------------------------------------- density guard


class DensityViolation(StrictModel):
    scope: Literal["per_kind", "global"]
    kind: PresentationIntentKind | None = None
    count: int = Field(ge=1, strict=True)
    observed_per_minute: float = Field(ge=0.0, strict=True)
    cap_per_minute: float = Field(ge=0.0, strict=True)

    @model_validator(mode="after")
    def require_scope_matches_kind(self) -> DensityViolation:
        if (self.scope == "per_kind") is (self.kind is None):
            raise PydanticCustomError(
                "violation_scope_kind",
                "per_kind violations name a kind; global violations carry none",
            )
        return self


class DensityReport(StrictModel):
    timeline_duration_seconds: float = Field(gt=0.0, strict=True)
    intent_count: int = Field(ge=0, strict=True)
    counts_by_kind: dict[PresentationIntentKind, int]
    violations: Annotated[tuple[DensityViolation, ...], BeforeValidator(to_tuple)] = ()


class DensityLimitExceededError(ValueError):
    """Intent density exceeded one or more profile caps; carries the report."""

    def __init__(self, report: DensityReport) -> None:
        self.report = report
        parts = [
            f"{violation.kind or 'global'}: {violation.count} intents = "
            f"{violation.observed_per_minute:.2f}/min over cap "
            f"{violation.cap_per_minute:.2f}/min"
            for violation in report.violations
        ]
        super().__init__("presentation density over profile caps: " + "; ".join(parts))


class ProfileRangeViolationError(ValueError):
    """An intent parameter sits outside the profile's allowed range."""


def check_density(
    intents: Sequence[PresentationIntentV2],
    profile: ChannelPresentationProfile,
    *,
    timeline_duration_seconds: float,
) -> DensityReport:
    """Validate intent density against the profile caps; reject when over.

    Purely a guard: it never modifies the intents and never introduces new
    effects to satisfy a Gate quota (that path does not exist here).
    """

    if timeline_duration_seconds <= 0:
        raise ValueError("timeline_duration_seconds must be positive")
    minutes = timeline_duration_seconds / 60.0
    counts: dict[PresentationIntentKind, int] = {}
    for intent in intents:
        counts[intent.kind] = counts.get(intent.kind, 0) + 1
    violations: list[DensityViolation] = []
    for kind in ALL_KINDS:
        count = counts.get(kind, 0)
        if count == 0:
            continue
        cap = profile.density.per_kind[kind]
        observed = count / minutes
        if observed > cap:
            violations.append(
                DensityViolation(
                    scope="per_kind",
                    kind=kind,
                    count=count,
                    observed_per_minute=observed,
                    cap_per_minute=cap,
                )
            )
    total = sum(counts.values())
    global_observed = total / minutes
    if global_observed > profile.density.global_per_minute:
        violations.append(
            DensityViolation(
                scope="global",
                kind=None,
                count=total,
                observed_per_minute=global_observed,
                cap_per_minute=profile.density.global_per_minute,
            )
        )
    report = DensityReport(
        timeline_duration_seconds=timeline_duration_seconds,
        intent_count=total,
        counts_by_kind=dict(sorted(counts.items())),
        violations=tuple(violations),
    )
    if violations:
        raise DensityLimitExceededError(report)
    return report


def _punch_in_scale(params: StrictModel) -> float:
    match params:
        case PunchInParams(scale=scale):
            return scale
        case _:
            raise TypeError("punch-in scale requires punch-in params")


def validate_against_profile(
    intents: Sequence[PresentationIntentV2], profile: ChannelPresentationProfile
) -> None:
    """Reject intent params outside profile ranges (punch-in scale today)."""

    for intent in intents:
        if intent.kind != "emphasis_punch_in":
            continue
        scale = _punch_in_scale(intent.params)
        if not profile.punch_in_range.covers(scale):
            raise ProfileRangeViolationError(
                f"emphasis_punch_in scale {scale} outside profile punch_in_range "
                f"[{profile.punch_in_range.min}, {profile.punch_in_range.max}]"
            )


# ------------------------------------------------------- task-31 IR + loader


def attach_presentation_intents(
    ir: TimelineIrV2, intents: Sequence[PresentationIntentV2]
) -> TimelineIrV2:
    """Task-31 seam: a NEW IR whose presentation_intent_refs name these intents."""

    refs = tuple(intent.intent_id for intent in intents)
    if len(set(refs)) != len(refs):
        raise ValueError("presentation intent ids must be unique")
    return ir.model_copy(update={"presentation_intent_refs": refs})


_DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "production-kit" /
    "presentation-profile-default.json"
)


def load_default_profile(path: Path = _DEFAULT_PROFILE_PATH) -> ChannelPresentationProfile:
    """Load the shipped conservative default presentation profile."""

    return ChannelPresentationProfile.model_validate_json(path.read_text(encoding="utf-8"))


__all__ = [
    "ALL_KINDS",
    "AudioPolicy",
    "BrandAssets",
    "ChannelPresentationProfile",
    "ChapterCardParams",
    "ClosedRange",
    "ColorPolicy",
    "CutawayParams",
    "DensityLimitExceededError",
    "DensityLimits",
    "DensityReport",
    "DensityViolation",
    "IntroOutroRules",
    "KeywordTextParams",
    "LicenseEvidence",
    "LowerThirdParams",
    "MotionTransitionParams",
    "PictureInPictureParams",
    "PresentationIntentKind",
    "PresentationIntentV2",
    "ProfileRangeViolationError",
    "PunchInParams",
    "PunchInRange",
    "ScreenHighlightParams",
    "SfxAccentParams",
    "SfxPolicy",
    "SimpleDissolveParams",
    "SubtitleStyle",
    "TitleChoices",
    "TransitionPreferences",
    "attach_presentation_intents",
    "check_density",
    "load_default_profile",
    "validate_against_profile",
]
