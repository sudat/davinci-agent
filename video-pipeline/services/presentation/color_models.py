"""Color presentation models (Todo 60): the profile-driven color section.

The compiled color section carries the profile's PROJECT color settings
(the working space plus the brand color tokens that are the declared A/B
difference dimensions) and, per video source, the camera preset SELECTED
from the source manifest's DECLARED camera/color metadata — with the exact
preset file (LUT/DRX) sha256 and tool version bound in. Selection is never
a guess: unknown cameras and missing colorimetry never compile, and the
models forbid per-shot grading payloads entirely.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.contracts.serialization import GENESIS_SHA256, canonical_json_bytes

type PresetKind = Literal["lut", "drx", "preset"]
type SelectionBasis = Literal["camera_make_model", "declared_colorimetry"]
type WorkingSpace = Literal["rec709"]
type ColorRung = Literal["project_setting", "external_validation"]

HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-F]{6}$", strict=True)]


def _tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class DeclaredCameraColor(StrictModel):
    """What one source DECLARED about its camera/colorimetry (never guessed)."""

    source_id: Identifier
    camera_make: str | None = None
    camera_model: str | None = None
    color_space: str = Field(min_length=1)
    color_transfer: str = Field(min_length=1)
    color_primaries: str = Field(min_length=1)
    color_range: str | None = None


class CameraPresetEntry(StrictModel):
    """One registered camera preset, pinned to exact file bytes + tool."""

    preset_id: Identifier
    kind: PresetKind
    camera_make: str | None = None
    camera_model: str | None = None
    color_space: str = Field(min_length=1)
    color_transfer: str = Field(min_length=1)
    color_primaries: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    file_sha256: Sha256
    tool_version: str = Field(min_length=1)


class CameraPresetCatalog(StrictModel):
    """The deterministic selection catalog: unique keys, no ambiguity."""

    presets: Annotated[tuple[CameraPresetEntry, ...], BeforeValidator(_tuple)] = (
        Field(min_length=1)
    )

    @model_validator(mode="after")
    def require_unique_selection_keys(self) -> CameraPresetCatalog:
        ids = [preset.preset_id for preset in self.presets]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("duplicate_preset", "preset ids must be unique")
        cameras = [
            (preset.camera_make, preset.camera_model)
            for preset in self.presets
            if preset.camera_make is not None and preset.camera_model is not None
        ]
        if len(set(cameras)) != len(cameras):
            raise PydanticCustomError(
                "duplicate_camera", "each camera maps to exactly one preset"
            )
        generic = [
            (preset.color_space, preset.color_transfer, preset.color_primaries)
            for preset in self.presets
            if preset.camera_make is None and preset.camera_model is None
        ]
        if len(set(generic)) != len(generic):
            raise PydanticCustomError(
                "duplicate_colorimetry",
                "each declared colorimetry maps to exactly one generic preset",
            )
        return self

    def entry_for(self, preset_id: str) -> CameraPresetEntry | None:
        return next(
            (preset for preset in self.presets if preset.preset_id == preset_id), None
        )


class SelectedCameraPreset(StrictModel):
    """The deterministic selection result, hash-bound to the preset file."""

    preset_id: Identifier
    kind: PresetKind
    selection_basis: SelectionBasis
    camera_make: str | None = None
    camera_model: str | None = None
    color_space: str = Field(min_length=1)
    color_transfer: str = Field(min_length=1)
    color_primaries: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    file_sha256: Sha256
    tool_version: str = Field(min_length=1)


class SourceColorBinding(StrictModel):
    """One source's declared colorimetry plus its selected preset."""

    source_id: Identifier
    declared: DeclaredCameraColor
    preset: SelectedCameraPreset

    @model_validator(mode="after")
    def require_binding_consistency(self) -> SourceColorBinding:
        if self.declared.source_id != self.source_id:
            raise PydanticCustomError(
                "binding_source", "binding source id must match the declared source"
            )
        if (
            self.preset.color_space,
            self.preset.color_transfer,
            self.preset.color_primaries,
        ) != (
            self.declared.color_space,
            self.declared.color_transfer,
            self.declared.color_primaries,
        ):
            raise PydanticCustomError(
                "binding_colorimetry",
                "the selected preset must match the declared colorimetry",
            )
        if self.preset.selection_basis == "camera_make_model" and (
            self.preset.camera_make != self.declared.camera_make
            or self.preset.camera_model != self.declared.camera_model
        ):
            raise PydanticCustomError(
                "binding_camera", "the preset camera must match the declared camera"
            )
        return self


class ColorProfileSection(StrictModel):
    """The compiled, hash-sealed color plan for one episode."""

    schema_version: Literal["color-profile-section-v1"] = "color-profile-section-v1"
    episode_id: Identifier
    profile_snapshot_sha256: Sha256
    working_space: WorkingSpace
    primary_hex: HexColor
    accent_hex: HexColor
    neutral_hex: HexColor
    sources: tuple[SourceColorBinding, ...] = Field(min_length=1)
    section_sha256: Sha256

    @model_validator(mode="after")
    def require_unique_sources(self) -> ColorProfileSection:
        ids = [binding.source_id for binding in self.sources]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError(
                "duplicate_source", "each video source binds exactly once"
            )
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    def content_hash(self) -> Sha256:
        zeroed = self.model_copy(update={"section_sha256": GENESIS_SHA256})
        return hashlib.sha256(canonical_json_bytes(zeroed)).hexdigest()

    def verify_hash(self) -> bool:
        return self.section_sha256 == self.content_hash()


class SettingBinding(StrictModel):
    """A live-verified project color setting the rung applied."""

    setting_name: str = Field(min_length=1)
    probe_report_sha256: Sha256


class ColorSection(StrictModel):
    """The package color section: the chosen application rung."""

    schema_version: Literal["color-section-v1"] = "color-section-v1"
    rung: ColorRung
    reason: str = Field(min_length=1)
    profile_section_sha256: Sha256
    working_space: WorkingSpace
    setting_binding: SettingBinding | None = None

    @model_validator(mode="after")
    def require_rung_payload(self) -> ColorSection:
        if self.rung == "project_setting" and self.setting_binding is None:
            raise PydanticCustomError(
                "rung_payload",
                "the project-setting rung requires a probe-bound setting",
            )
        if self.rung == "external_validation" and self.setting_binding is not None:
            raise PydanticCustomError(
                "rung_payload", "the external rung carries no setting binding"
            )
        return self


class ColorTargets(StrictModel):
    """The declared output colorimetry the external validation binds to."""

    color_space: Literal["bt709"] = "bt709"
    color_transfer: Literal["bt709"] = "bt709"
    color_primaries: Literal["bt709"] = "bt709"
    color_range: Literal["tv"] = "tv"


__all__ = [
    "CameraPresetCatalog",
    "CameraPresetEntry",
    "ColorProfileSection",
    "ColorRung",
    "ColorSection",
    "ColorTargets",
    "DeclaredCameraColor",
    "PresetKind",
    "SelectedCameraPreset",
    "SelectionBasis",
    "SettingBinding",
    "SourceColorBinding",
    "WorkingSpace",
]
