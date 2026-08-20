"""The Phase-3 color section of a Resolve Package (Todo 60).

The package carries the color section VERBATIM after re-verifying, from
the package's own inputs, that the section binds to exactly this compiled
color profile section: the seal must match, and every bound preset file
must still hash to its declared bytes (stale LUTs are typed drift).
Resolve-specific application (project-level color settings where
live-readable) lives behind the probe findings; this section is
descriptive, NLE-neutral, and never a per-shot grade instruction.
"""

from __future__ import annotations

from pathlib import Path

from services.presentation.color_models import (
    CameraPresetCatalog,
    CameraPresetEntry,
    ColorProfileSection,
    ColorSection,
)
from services.presentation.color_profile import ColorProfileError, verify_color_section
from services.resolve_adapter.errors import COLOR_SECTION_DRIFT, PackageCompileError


def _section_catalog(section: ColorProfileSection) -> CameraPresetCatalog:
    entries = tuple(
        CameraPresetEntry(
            preset_id=binding.preset.preset_id,
            kind=binding.preset.kind,
            camera_make=binding.preset.camera_make,
            camera_model=binding.preset.camera_model,
            color_space=binding.preset.color_space,
            color_transfer=binding.preset.color_transfer,
            color_primaries=binding.preset.color_primaries,
            file_path=binding.preset.file_path,
            file_sha256=binding.preset.file_sha256,
            tool_version=binding.preset.tool_version,
        )
        for binding in section.sources
    )
    return CameraPresetCatalog(presets=entries)


def attach_color_section(
    color: ColorSection, section: ColorProfileSection
) -> ColorSection:
    """Verify the binding to this compiled section; typed drift otherwise."""

    if color.profile_section_sha256 != section.section_sha256:
        raise PackageCompileError(
            COLOR_SECTION_DRIFT,
            f"color section binds {color.profile_section_sha256} but the compiled "
            f"section is {section.section_sha256}",
        )
    try:
        verify_color_section(section, _section_catalog(section), assets_root=Path())
    except ColorProfileError as error:
        raise PackageCompileError(
            COLOR_SECTION_DRIFT, f"color section no longer verifies: {error}"
        ) from error
    return color


__all__ = ["attach_color_section"]
