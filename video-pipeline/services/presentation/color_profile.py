"""Project Color Management compilation (Todo 60): selection, never a guess.

Compiles :class:`ColorProfileSection` from the resolved presentation
profile (project working space + brand color dims) and each source's
DECLARED camera/color metadata. Selection rules are DECLARED and
deterministic: an exact camera make/model match wins; otherwise exactly
one colorimetry match; anything else is the typed blocking/human route —
never a guessed preset. HDR declares against the rec709 SDR working space,
absent/changed preset file bytes, and any per-shot model-driven grade
request are all typed refusals.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.foundation_io import sha256_file
from services.ingest.models import SourceManifest, VideoStreamRecord
from services.presentation.color_models import (
    CameraPresetCatalog,
    ColorProfileSection,
    DeclaredCameraColor,
    SelectedCameraPreset,
    SourceColorBinding,
)
from services.spike.gate_models import CapabilityMatrix

if TYPE_CHECKING:
    from services.presentation.models import ResolvedPresentationProfile

HDR_TRANSFERS: Final[frozenset[str]] = frozenset({"smpte2084", "arib-std-b67"})
COLOR_FINDING: Final = "project-color-setting-roundtrip"
COLOR_CAPABILITY: Final = "project_color_settings"


class ColorProfileError(ValueError):
    """Typed blocking color failure; ``code`` is the machine cause."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class SettingSupport:
    verified: bool
    evidence_sha: str | None = None


def declared_color_from_manifest(
    manifest: SourceManifest, *, camera_make: str | None, camera_model: str | None
) -> DeclaredCameraColor:
    """Build the declared color record from the source manifest's own fields."""

    if manifest.eligibility.verdict != "supported":
        raise ColorProfileError(
            "source_ineligible", f"source {manifest.artifact_id} is not eligible"
        )
    video = next(
        (
            stream
            for stream in manifest.streams
            if isinstance(stream, VideoStreamRecord)
        ),
        None,
    )
    if video is None:
        raise ColorProfileError(
            "missing_video_stream", f"source {manifest.artifact_id} has no video stream"
        )
    if video.hdr.dolby_vision_rpu or video.hdr.hdr10_mastering_display:
        raise ColorProfileError(
            "hdr_unsupported",
            f"source {manifest.artifact_id} declares HDR signaling; the rec709 "
            "SDR working space has no supported HDR conversion path",
        )
    color_space = video.color_space or ""
    color_transfer = video.color_transfer or ""
    color_primaries = video.color_primaries or ""
    missing = [
        name
        for name, value in (
            ("color_space", color_space),
            ("color_transfer", color_transfer),
            ("color_primaries", color_primaries),
        )
        if not value
    ]
    if missing:
        raise ColorProfileError(
            "missing_color_metadata",
            f"source {manifest.artifact_id} declares no {', '.join(missing)}; "
            "selection needs the declared colorimetry (human route, no guess)",
        )
    if color_transfer in HDR_TRANSFERS:
        raise ColorProfileError(
            "hdr_unsupported",
            f"source {manifest.artifact_id} declares HDR transfer "
            f"{color_transfer}; mismatched against the rec709 SDR profile",
        )
    return DeclaredCameraColor(
        source_id=manifest.artifact_id,
        camera_make=camera_make,
        camera_model=camera_model,
        color_space=color_space,
        color_transfer=color_transfer,
        color_primaries=color_primaries,
        color_range=video.color_range,
    )


def _verify_preset_bytes(entry_path: Path, expected_sha: str) -> str:
    try:
        observed = sha256_file(entry_path)
    except OSError as error:
        raise ColorProfileError(
            "lut_missing", f"preset bytes unreadable at {entry_path}: {error}"
        ) from error
    if observed != expected_sha:
        raise ColorProfileError(
            "lut_changed",
            f"preset bytes drifted from the declared hash at {entry_path}",
        )
    return observed


def select_camera_preset(
    declared: DeclaredCameraColor,
    catalog: CameraPresetCatalog,
    *,
    assets_root: Path,
) -> SelectedCameraPreset:
    """Deterministic selection from DECLARED metadata; typed otherwise."""

    if declared.color_transfer in HDR_TRANSFERS:
        raise ColorProfileError(
            "hdr_unsupported",
            f"source {declared.source_id} declares HDR transfer "
            f"{declared.color_transfer}; no unsupported HDR conversion is attempted",
        )
    entry = None
    basis = "declared_colorimetry"
    if declared.camera_make is not None and declared.camera_model is not None:
        entry = next(
            (
                preset
                for preset in catalog.presets
                if preset.camera_make == declared.camera_make
                and preset.camera_model == declared.camera_model
            ),
            None,
        )
        basis = "camera_make_model"
    if entry is None and basis == "declared_colorimetry":
        entry = next(
            (
                preset
                for preset in catalog.presets
                if preset.camera_make is None
                and preset.camera_model is None
                and (
                    preset.color_space,
                    preset.color_transfer,
                    preset.color_primaries,
                )
                == (
                    declared.color_space,
                    declared.color_transfer,
                    declared.color_primaries,
                )
            ),
            None,
        )
    if entry is None:
        raise ColorProfileError(
            "unknown_camera",
            f"source {declared.source_id} declares "
            f"camera={declared.camera_make}/{declared.camera_model} "
            f"colorimetry={declared.color_space}/{declared.color_transfer}/"
            f"{declared.color_primaries}; no registered preset matches — "
            "blocking for human preset registration, never a guess",
        )
    path = Path(entry.file_path)
    resolved = path if path.is_absolute() else assets_root / path
    _verify_preset_bytes(resolved, entry.file_sha256)
    return SelectedCameraPreset(
        preset_id=entry.preset_id,
        kind=entry.kind,
        selection_basis=basis,
        camera_make=entry.camera_make,
        camera_model=entry.camera_model,
        color_space=entry.color_space,
        color_transfer=entry.color_transfer,
        color_primaries=entry.color_primaries,
        file_path=entry.file_path,
        file_sha256=entry.file_sha256,
        tool_version=entry.tool_version,
    )


def compile_color_profile(
    profile: ResolvedPresentationProfile,
    declared_sources: Mapping[str, DeclaredCameraColor],
    catalog: CameraPresetCatalog,
    *,
    assets_root: Path,
    per_shot_grade_request: str | None = None,
) -> ColorProfileSection:
    """Compile the hash-sealed color section; every refusal is typed."""

    if per_shot_grade_request is not None:
        raise ColorProfileError(
            "per_shot_grade_refused",
            f"per-shot model-driven grading is out of scope (MVP refuses): "
            f"{per_shot_grade_request[:120]}",
        )
    if not declared_sources:
        raise ColorProfileError(
            "missing_color_metadata", "no sources declared any color metadata"
        )
    color = profile.color_profile
    bindings = tuple(
        SourceColorBinding(
            source_id=source_id,
            declared=declared,
            preset=select_camera_preset(declared, catalog, assets_root=assets_root),
        )
        for source_id, declared in sorted(declared_sources.items())
    )
    draft = ColorProfileSection(
        episode_id=profile.episode_id,
        profile_snapshot_sha256=profile.content_hash(),
        working_space=color.working_space,
        primary_hex=color.primary_hex,
        accent_hex=color.accent_hex,
        neutral_hex=color.neutral_hex,
        sources=bindings,
        section_sha256="0" * 64,
    )
    return draft.model_copy(update={"section_sha256": draft.content_hash()})


def verify_color_section(
    section: ColorProfileSection,
    catalog: CameraPresetCatalog,
    *,
    assets_root: Path,
) -> None:
    """Re-verify the seal and every preset hash; drift is typed."""

    if not section.verify_hash():
        raise ColorProfileError(
            "section_seal_invalid", "the color section seal does not match its bytes"
        )
    for binding in section.sources:
        entry = catalog.entry_for(binding.preset.preset_id)
        if entry is None:
            raise ColorProfileError(
                "preset_unregistered",
                f"preset {binding.preset.preset_id} is not in the registered catalog",
            )
        path = Path(entry.file_path)
        _verify_preset_bytes(
            path if path.is_absolute() else assets_root / path, entry.file_sha256
        )


def color_setting_support_from_matrix(matrix_path: Path) -> SettingSupport:
    """Honest setting rung support from published probe findings."""

    try:
        matrix = CapabilityMatrix.model_validate_json(matrix_path.read_bytes())
    except (OSError, ValueError):
        return SettingSupport(verified=False, evidence_sha=None)
    finding = next(
        (row for row in matrix.findings if row.finding == COLOR_FINDING), None
    )
    if finding is None or not finding.live_verified:
        return SettingSupport(verified=False, evidence_sha=None)
    return SettingSupport(
        verified=True,
        evidence_sha=finding.evidence_refs[0].sha256 if finding.evidence_refs else None,
    )


__all__ = [
    "COLOR_CAPABILITY",
    "COLOR_FINDING",
    "ColorProfileError",
    "SettingSupport",
    "color_setting_support_from_matrix",
    "compile_color_profile",
    "declared_color_from_manifest",
    "select_camera_preset",
    "verify_color_section",
]
