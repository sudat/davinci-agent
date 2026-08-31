"""Parity models (Todo 61): manifest-anchored Preview-vs-Final observations.

One side (preview or final) carries what that render was BUILT from (the
manifest anchor, item placements, assets, cue regions, shared derivative
hashes) plus what was MEASURED from its bytes (audio duration/loudness/
peak, color metadata and region statistics). Parity is declared-dimension
comparison under declared tolerances — never container byte equality.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel, to_tuple

type ParitySideKind = Literal["preview", "final"]

type ParityDim = Literal[
    "manifest_anchor",
    "items",
    "assets",
    "cues",
    "placements",
    "subtitle_regions",
    "shared_derivatives",
    "audio_duration",
    "audio_loudness",
    "audio_peak",
    "color_metadata",
    "color_statistics",
]

type ParityMismatchCode = Literal[
    "unexplained_difference",
    "missing_shared_derivative",
    "derivative_hash_mismatch",
    "invalid_comparison_basis",
]


type ParitySequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(to_tuple)]


class ItemPlacementObservation(StrictModel):
    """One editorial item's record placement as the side was built from."""

    item_id: Identifier
    record_start: int = Field(ge=0, strict=True)
    record_end: int = Field(gt=0, strict=True)


class PlacementObservation(StrictModel):
    """The manifest placement binding projected for exact comparison."""

    intro_start_frame: int = Field(ge=0, strict=True)
    intro_end_frame: int = Field(gt=0, strict=True)
    outro_start_frame: int = Field(ge=0, strict=True)
    outro_end_frame: int = Field(gt=0, strict=True)
    safe_area_margin_px: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_ordered_spans(self) -> PlacementObservation:
        if not (
            self.intro_start_frame < self.intro_end_frame
            <= self.outro_start_frame
            < self.outro_end_frame
        ):
            raise PydanticCustomError(
                "placement_order",
                "intro/outro placement spans must be ordered and non-overlapping",
            )
        return self


class CueRegionObservation(StrictModel):
    """One subtitle cue: text/timing plus the per-cue geometry/style it wore."""

    item_id: Identifier
    text: str = Field(min_length=1, strict=True)
    lines: ParitySequence[str] = Field(min_length=1)
    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(gt=0, strict=True)
    style_id: Identifier
    font_size_px: int = Field(gt=0, strict=True)
    margin_bottom_px: int = Field(ge=0, strict=True)
    primary_color_hex: str = Field(min_length=1, strict=True)


class AudioObservation(StrictModel):
    """Measured audio facts for one side (Todo-59 measured QC reuse)."""

    duration_samples: int = Field(gt=0, strict=True)
    integrated_mlufs: int | None = None
    peak_mb: int = Field(le=0, strict=True)
    channels: int = Field(gt=0, strict=True)


class ColorObservation(StrictModel):
    """Declared color metadata plus measured region statistics, one side."""

    color_space: str = Field(default="", max_length=32, strict=True)
    color_transfer: str = Field(default="", max_length=32, strict=True)
    color_primaries: str = Field(default="", max_length=32, strict=True)
    region_means: dict[str, int] = Field(default_factory=dict)


class ParitySide(StrictModel):
    """Everything one render anchored to and everything measured from it."""

    side: ParitySideKind
    manifest_sha256: Sha256
    profile_snapshot_sha256: Sha256
    items: ParitySequence[ItemPlacementObservation] = ()
    assets: dict[str, Sha256] = Field(default_factory=dict)
    placements: PlacementObservation | None = None
    cues: ParitySequence[CueRegionObservation] = ()
    derivatives: dict[str, Sha256] = Field(default_factory=dict)
    audio: AudioObservation | None = None
    color: ColorObservation | None = None


class ParityTolerances(StrictModel):
    """Declared tolerances; the ONLY basis for measured-dimension verdicts."""

    schema_version: Literal["parity-tolerances-v1"] = "parity-tolerances-v1"
    audio_duration_tolerance_samples: int = Field(gt=0, strict=True)
    audio_loudness_target_mlufs: int = Field(lt=0, strict=True)
    audio_loudness_tolerance_mlufs: int = Field(gt=0, le=3000, strict=True)
    audio_max_peak_mb: int = Field(lt=0, strict=True)
    audio_peak_tolerance_mb: int = Field(gt=0, strict=True)
    audio_channels: int = Field(gt=0, le=8, strict=True)
    color_region_tolerance: int = Field(gt=0, strict=True)


class ComparisonBasis(StrictModel):
    """The declared comparison surface; container bytes are never a basis."""

    basis_version: Literal["declared-dims-v1"] = "declared-dims-v1"
    dims: ParitySequence[ParityDim] = Field(min_length=1)
    container_bytes_only: bool = False


class ParityMismatch(StrictModel):
    """One typed parity failure along one declared dimension."""

    code: ParityMismatchCode
    dim: ParityDim | None = None
    detail: str = Field(min_length=1)
    diff: dict[str, str] = Field(default_factory=dict)


class ParityReport(StrictModel):
    """The comparator verdict; ``passed`` only with zero mismatches."""

    schema_version: Literal["parity-report-v1"] = "parity-report-v1"
    manifest_sha256: Sha256
    basis: ComparisonBasis
    dims_compared: ParitySequence[ParityDim] = ()
    derivative_hashes: dict[str, Sha256] = Field(default_factory=dict)
    mismatches: ParitySequence[ParityMismatch] = ()
    passed: bool

    def codes(self) -> tuple[str, ...]:
        return tuple(mismatch.code for mismatch in self.mismatches)


__all__ = [
    "AudioObservation",
    "ColorObservation",
    "ComparisonBasis",
    "CueRegionObservation",
    "ItemPlacementObservation",
    "ParityDim",
    "ParityMismatch",
    "ParityMismatchCode",
    "ParitySequence",
    "ParitySide",
    "ParitySideKind",
    "ParityTolerances",
    "PlacementObservation",
]
