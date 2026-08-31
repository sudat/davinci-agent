"""Presentation profile models (PRD 25): the presentation-only key set.

Layers are System < Genre < Channel < Episode < ApprovedOverride, mirroring
:mod:`services.config.models`. Only presentation keys live here: subtitle
style, color, audio branding, placement, and asset bindings. Resolution in
:mod:`services.presentation.profiles` is narrowing-only for the asset
catalog; the resolved snapshot is immutable and hash-bound over canonical
bytes.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel, to_tuple
from services.contracts.serialization import GENESIS_SHA256, canonical_json_bytes

type Sequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(to_tuple)]

type PresentationAssetKind = Literal["intro", "logo", "outro", "overlay", "se", "tone"]
type Anchor = Literal["top-left", "top-right", "bottom-left", "bottom-right"]

PHASE_3_PRESENTATION_KINDS: tuple[PresentationAssetKind, ...] = (
    "intro",
    "logo",
    "outro",
    "overlay",
    "se",
    "tone",
)

HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-F]{6}$", strict=True)]


class SubtitleStyleConfig(StrictModel):
    """Subtitle style parameters; text content never lives in a profile."""

    style_id: Identifier
    font_family: str = Field(min_length=1)
    font_size_px: int = Field(ge=8, le=200, strict=True)
    primary_color_hex: HexColor
    outline_color_hex: HexColor
    outline_width_px: int = Field(ge=0, le=12, strict=True)
    margin_bottom_px: int = Field(ge=0, le=480, strict=True)
    background_opacity_percent: int = Field(ge=0, le=100, strict=True)


class ColorProfileConfig(StrictModel):
    profile_id: Identifier
    working_space: Literal["rec709"]
    primary_hex: HexColor
    accent_hex: HexColor
    neutral_hex: HexColor


class AudioBrandConfig(StrictModel):
    tone_hz: int = Field(ge=80, le=8000, strict=True)
    intro_tone_hz: int = Field(ge=80, le=8000, strict=True)
    outro_tone_hz: int = Field(ge=80, le=8000, strict=True)
    se_hz: int = Field(ge=80, le=8000, strict=True)
    tone_duration_ms: int = Field(gt=0, strict=True)
    se_duration_ms: int = Field(gt=0, strict=True)
    sample_rate_hz: int = Field(gt=0, strict=True)


class PlacementConfig(StrictModel):
    intro_duration_frames: int = Field(gt=0, strict=True)
    outro_duration_frames: int = Field(gt=0, strict=True)
    logo_anchor: Anchor
    overlay_anchor: Anchor
    safe_area_margin_px: int = Field(ge=0, strict=True)


class AssetBinding(StrictModel):
    """One presentation slot bound to a registry asset id."""

    kind: PresentationAssetKind
    asset_id: Identifier


def _canonical_bindings(
    bindings: tuple[AssetBinding, ...],
) -> tuple[AssetBinding, ...]:
    kinds = [binding.kind for binding in bindings]
    if len(set(kinds)) != len(kinds):
        raise PydanticCustomError("duplicate_binding", "one binding per asset kind")
    return tuple(sorted(bindings, key=lambda binding: binding.kind))


def _require_complete_bindings(bindings: AssetBindings) -> None:
    kinds = {binding.kind for binding in bindings}
    if kinds != set(PHASE_3_PRESENTATION_KINDS):
        raise PydanticCustomError(
            "binding_inventory",
            "bindings must cover exactly the six presentation kinds",
        )


def _canonical_catalog(catalog: tuple[Identifier, ...]) -> tuple[Identifier, ...]:
    return tuple(sorted(set(catalog)))


type AssetBindings = Annotated[
    tuple[AssetBinding, ...],
    BeforeValidator(to_tuple),
    AfterValidator(_canonical_bindings),
]
type AssetCatalog = Annotated[
    tuple[Identifier, ...], BeforeValidator(to_tuple), AfterValidator(_canonical_catalog)
]


class SystemPresentationProfile(StrictModel):
    """The floor layer: full presentation defaults and the asset catalog."""

    schema_version: Literal["system-presentation-v1"] = "system-presentation-v1"
    asset_catalog: AssetCatalog = Field(min_length=1)
    subtitle_style: SubtitleStyleConfig
    color_profile: ColorProfileConfig
    audio: AudioBrandConfig
    placement: PlacementConfig
    asset_bindings: AssetBindings

    @model_validator(mode="after")
    def require_complete_bindings(self) -> SystemPresentationProfile:
        _require_complete_bindings(self.asset_bindings)
        return self


class _PresentationKeys(StrictModel):
    """Optional overrides; higher layers may only narrow the catalog."""

    subtitle_style: SubtitleStyleConfig | None = None
    color_profile: ColorProfileConfig | None = None
    audio: AudioBrandConfig | None = None
    placement: PlacementConfig | None = None
    asset_bindings: AssetBindings | None = None
    asset_catalog: AssetCatalog | None = None


class GenrePresentationProfile(_PresentationKeys):
    genre_id: Identifier


class ChannelPresentationProfile(_PresentationKeys):
    channel_id: Identifier


class EpisodePresentationProfile(_PresentationKeys):
    episode_id: Identifier
    genre: Identifier | None = None
    channel: Identifier | None = None


class ApprovedPresentationOverride(_PresentationKeys):
    override_id: Identifier
    approved_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ResolvedPresentationProfile(StrictModel):
    """Immutable Job-start presentation snapshot, hash-bound over canonical bytes."""

    schema_version: Literal["resolved-presentation-profile-v1"] = (
        "resolved-presentation-profile-v1"
    )
    episode_id: Identifier
    layers_applied: tuple[Identifier, ...]
    asset_catalog: AssetCatalog
    subtitle_style: SubtitleStyleConfig
    color_profile: ColorProfileConfig
    audio: AudioBrandConfig
    placement: PlacementConfig
    asset_bindings: AssetBindings
    profile_snapshot_sha256: Sha256

    @model_validator(mode="after")
    def require_complete_bindings(self) -> ResolvedPresentationProfile:
        _require_complete_bindings(self.asset_bindings)
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    def content_hash(self) -> Sha256:
        zeroed = self.model_copy(update={"profile_snapshot_sha256": GENESIS_SHA256})
        return hashlib.sha256(canonical_json_bytes(zeroed)).hexdigest()

    def verify_hash(self) -> bool:
        return self.profile_snapshot_sha256 == self.content_hash()


__all__ = [
    "PHASE_3_PRESENTATION_KINDS",
    "Anchor",
    "ApprovedPresentationOverride",
    "AssetBinding",
    "AssetBindings",
    "AssetCatalog",
    "AudioBrandConfig",
    "ChannelPresentationProfile",
    "ColorProfileConfig",
    "EpisodePresentationProfile",
    "GenrePresentationProfile",
    "PlacementConfig",
    "PresentationAssetKind",
    "ResolvedPresentationProfile",
    "SubtitleStyleConfig",
    "SystemPresentationProfile",
]
