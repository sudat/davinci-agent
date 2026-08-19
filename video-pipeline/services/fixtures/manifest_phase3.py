"""Phase-3 fixture manifest models (two rights-safe generated brand packages).

Each manifest freezes one generated brand package: self-owned byte assets
produced by the pinned ffmpeg from lavfi/aevalsrc sources only, exact
owner-created rights metadata, the SHARED editorial base derived from the
frozen Phase-1 Reference ``p1-ref-01-clean-ja``, and the declared
presentation-diff dimensions against the sibling brand. Every expectation is
pre-registered here; nothing is derived from implementation output.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.fixtures.manifest import Sequence  # noqa: TC001 (pydantic runtime)
from services.fixtures.manifest_phase2 import (  # noqa: TC001 (pydantic runtime)
    BaseRecordRow,
    MediaBindingSpec,
)
from services.foundation_io import canonical_model_bytes

PHASE_3_FIXTURE_IDS: tuple[str, ...] = ("p3-brand-a", "p3-brand-b")
PHASE_3_EDITORIAL_DERIVED_FROM = "p1-ref-01-clean-ja"
PHASE_3_SHARED_EDIT_SOURCE_ID = "p1-edit-p1-ref-01-clean-ja"
PHASE_3_ASSET_KINDS: tuple[str, ...] = ("intro", "logo", "outro", "overlay", "se", "tone")

type AssetKind = Literal["intro", "logo", "outro", "overlay", "se", "tone"]
HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-F]{6}$", strict=True)]

ALLOWED_RECIPE_SOURCES: tuple[str, ...] = ("color=", "testsrc2", "aevalsrc")


class AssetRights(StrictModel):
    """Rights metadata for one generated asset; third-party claims refuse."""

    license: Literal["owner-created"]
    holder: Literal["pipeline-fixture-generator"]
    generated_by: Literal["pinned-ffmpeg-lavfi"]
    third_party_material: Literal["none"]
    production_brand_claimed: Literal[False] = False


class BrandAssetSpec(StrictModel):
    kind: AssetKind
    path: str
    sha256: Sha256
    recipe: Sequence[str] = Field(min_length=4)
    rights: AssetRights

    @model_validator(mode="after")
    def require_lavfi_only_recipe(self) -> BrandAssetSpec:
        if self.recipe[0] != "{ffmpeg}" or self.recipe[-1] != "{out}":
            raise PydanticCustomError("recipe_shape", "recipe must call the pinned ffmpeg")
        inputs = [
            self.recipe[index + 1] for index, element in enumerate(self.recipe) if element == "-i"
        ]
        if not inputs or not all(
            any(source in value for source in ALLOWED_RECIPE_SOURCES) for value in inputs
        ):
            raise PydanticCustomError(
                "recipe_source", "recipe inputs must be lavfi/aevalsrc sources only"
            )
        if any(isinstance(element, str) and "://" in element for element in self.recipe):
            raise PydanticCustomError("recipe_network", "network sources are forbidden")
        return self


class SubtitleStyleSpec(StrictModel):
    style_id: Identifier
    font_family: Literal["fixture-generic-sans"]
    font_size_px: int = Field(ge=8, le=200, strict=True)
    primary_color_hex: HexColor
    outline_color_hex: HexColor
    outline_width_px: int = Field(ge=0, le=12, strict=True)
    margin_bottom_px: int = Field(ge=0, le=480, strict=True)
    background_opacity_percent: int = Field(ge=0, le=100, strict=True)


class ColorProfileSpec(StrictModel):
    profile_id: Identifier
    working_space: Literal["rec709"]
    primary_hex: HexColor
    accent_hex: HexColor
    neutral_hex: HexColor


class AudioBrandSpec(StrictModel):
    tone_hz: int = Field(ge=80, le=8000, strict=True)
    intro_tone_hz: int = Field(ge=80, le=8000, strict=True)
    outro_tone_hz: int = Field(ge=80, le=8000, strict=True)
    se_hz: int = Field(ge=80, le=8000, strict=True)
    tone_duration_ms: Literal[3000]
    se_duration_ms: Literal[500]
    sample_rate_hz: Literal[48000]


class PlacementSpec(StrictModel):
    """Presentation placement geometry; identical between the two brands."""

    intro_duration_frames: Literal[30]
    outro_duration_frames: Literal[30]
    logo_anchor: Literal["bottom-right"]
    overlay_anchor: Literal["top-left"]
    safe_area_margin_px: Literal[48]


class EditorialBase(StrictModel):
    """The shared editorial structure; byte-identical between the brands."""

    derived_from: Literal["p1-ref-01-clean-ja"]
    source_id: Literal["p1-edit-p1-ref-01-clean-ja"]
    total_frames: Literal[600]
    frame_rate_num: Literal[30]
    frame_rate_den: Literal[1]
    audio_sample_rate: Literal[48000]
    base_records: Sequence[BaseRecordRow] = Field(min_length=1)
    declared_media: MediaBindingSpec
    editorial_structure_sha256: Sha256

    @model_validator(mode="after")
    def require_declared_structure_hash(self) -> EditorialBase:
        if hashlib.sha256(editorial_structure_bytes(self)).hexdigest() != (
            self.editorial_structure_sha256
        ):
            raise PydanticCustomError(
                "editorial_hash", "declared editorial structure hash does not match content"
            )
        if self.declared_media.duration_frames != self.total_frames:
            raise PydanticCustomError("media_extent", "declared media must cover the extent")
        return self


class PresentationSpec(StrictModel):
    assets: Sequence[BrandAssetSpec] = Field(min_length=6)
    subtitle_style: SubtitleStyleSpec
    color_profile: ColorProfileSpec
    audio: AudioBrandSpec
    placement: PlacementSpec

    @model_validator(mode="after")
    def require_full_inventory_and_consistent_tones(self) -> PresentationSpec:
        kinds = tuple(asset.kind for asset in self.assets)
        if len(set(kinds)) != len(kinds) or set(kinds) != set(PHASE_3_ASSET_KINDS):
            raise PydanticCustomError(
                "asset_inventory", "assets must cover exactly the six canonical kinds"
            )
        by_kind = {asset.kind: asset for asset in self.assets}
        tones = {
            "tone": self.audio.tone_hz,
            "se": self.audio.se_hz,
            "intro": self.audio.intro_tone_hz,
            "outro": self.audio.outro_tone_hz,
        }
        for kind, hz in tones.items():
            if f"*{hz}*" not in "".join(by_kind[kind].recipe):
                raise PydanticCustomError(
                    "tone_binding",
                    "declared {kind} tone must match its frozen recipe",
                    {"kind": kind},
                )
        return self


class DeclaredPresentationDiff(StrictModel):
    """Pre-registered A/B diff dimensions; every listed value must differ."""

    dimensions: Sequence[str] = Field(min_length=1)
    editorial_invariant: Literal[True] = True


class Phase3FixtureManifest(StrictModel):
    schema_version: Literal["phase-3-fixture-manifest-v1"]
    phase: Literal["phase-3"]
    fixture_id: str
    fixture_only: Literal[True]
    expectation_basis: Literal["pre-registered-declared-presentation-diff"]
    brand_id: Identifier
    editorial: EditorialBase
    presentation: PresentationSpec
    declared_diff: DeclaredPresentationDiff

    @model_validator(mode="after")
    def require_canonical_brand_binding(self) -> Phase3FixtureManifest:
        if self.fixture_id not in PHASE_3_FIXTURE_IDS:
            raise PydanticCustomError("fixture_id", "fixture id must be a phase-3 brand")
        if self.brand_id != self.fixture_id.removeprefix("p3-"):
            raise PydanticCustomError("brand_id", "brand id must extend the fixture id")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_model_bytes(self)


def editorial_structure_bytes(base: EditorialBase) -> bytes:
    projection = base.model_dump(mode="json", exclude={"editorial_structure_sha256"})
    return json.dumps(
        projection, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


__all__ = [
    "PHASE_3_ASSET_KINDS",
    "PHASE_3_EDITORIAL_DERIVED_FROM",
    "PHASE_3_FIXTURE_IDS",
    "PHASE_3_SHARED_EDIT_SOURCE_ID",
    "AssetKind",
    "AssetRights",
    "AudioBrandSpec",
    "BrandAssetSpec",
    "ColorProfileSpec",
    "DeclaredPresentationDiff",
    "EditorialBase",
    "Phase3FixtureManifest",
    "PlacementSpec",
    "PresentationSpec",
    "SubtitleStyleSpec",
    "editorial_structure_bytes",
]
