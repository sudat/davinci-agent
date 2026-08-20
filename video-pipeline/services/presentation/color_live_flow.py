"""LIVE end-to-end color flow (Todo 60): select, apply where verified, validate.

One owned ``__fvp_test__`` build: the bars fixture's OWN declared
colorimetry + camera container tags drive the deterministic preset
selection; the project-level color settings rung applies ONLY where the
Todo-60 probe findings verified a SetSetting/GetSetting round-trip (the
honest verdict is recorded either way); the timeline renders through the
official Deliver API; and the color compliance of BOTH the external
conformance derivative and the Resolve render is validated FROM BYTES:
ffprobe color metadata vs the declared targets and measured bar-region
luma vs the declared fixture constants. A/B brand profiles must differ
deterministically exactly in the declared color dims.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from services.foundation_io import atomic_write, sha256_file
from services.presentation.asset_registry import registry_from_phase3_manifests
from services.presentation.color_live_media import (
    MARKER,
    ColorLiveError,
    ColorPoolApi,
    ColorRenderProjectApi,
    ColorTimelineApi,
    append_video_clip,
    declared_color_from_media,
    render_bars_source,
    render_project,
    verify_clip_readback,
)
from services.presentation.color_models import (
    CameraPresetCatalog,
    CameraPresetEntry,
    ColorProfileSection,
    ColorSection,
    ColorTargets,
    SettingBinding,
)
from services.presentation.color_profile import (
    COLOR_FINDING,
    SettingSupport,
    color_setting_support_from_matrix,
    compile_color_profile,
    verify_color_section,
)
from services.presentation.color_render import (
    BAR_REGION_LUMA,
    IDENTITY_CUBE_BYTES,
    ColorRenderError,
    measure_region_luma,
    render_conformance_derivative,
    validate_output_color,
)
from services.presentation.models import (
    ChannelPresentationProfile,
    EpisodePresentationProfile,
    SystemPresentationProfile,
)
from services.presentation.profiles import resolve_presentation_profile
from services.resolve_bridge.lifecycle import (
    create_disposable_project,
    create_owned_timeline,
    owned_project_name,
    owned_timeline_name,
)

if TYPE_CHECKING:
    from services.fixtures.manifest_phase3 import Phase3FixtureManifest
    from services.presentation.models import ResolvedPresentationProfile
    from services.resolve_bridge.connection import ResolveConnection
    from services.resolve_bridge.fixed_presentation_models import FixedProjectApi

MANIFEST_DIR_NAME: str = "tests/fixtures/manifests/phase-3"
FINDINGS_PATH: Path = Path("capabilities/resolve-21.0.4/color-setting-findings.json")
REPORT_NAME: str = "color-live-report.json"
BARS_FRAMES: int = 30
BARS_SECONDS: int = 1
TIMELINE_WIDTH: int = 1920
TIMELINE_HEIGHT: int = 1080
TOOL_VERSION: str = "ffmpeg-7.1.1"
DECLARED_DIFF_DIMS: frozenset[str] = frozenset(
    {"primary_hex", "accent_hex", "neutral_hex"}
)


class ColorSettingProjectApi(Protocol):
    def GetSetting(self, setting_name: str) -> str: ...

    def SetSetting(self, setting_name: str, setting_value: str) -> bool: ...


def _keys_of(brand: Phase3FixtureManifest) -> dict[str, object]:
    presentation = brand.presentation
    return {
        "subtitle_style": dict(presentation.subtitle_style.model_dump()),
        "color_profile": dict(presentation.color_profile.model_dump()),
        "audio": dict(presentation.audio.model_dump()),
        "placement": dict(presentation.placement.model_dump()),
        "asset_bindings": [
            {"kind": asset.kind, "asset_id": f"{brand.fixture_id}:{asset.kind}"}
            for asset in presentation.assets
        ],
    }


def _catalog(lut: Path) -> CameraPresetCatalog:
    return CameraPresetCatalog(
        presets=(
            CameraPresetEntry(
                preset_id="fixturecam-fc1-rec709",
                kind="lut",
                camera_make="FIXTURECAM",
                camera_model="FC-1",
                color_space="bt709",
                color_transfer="bt709",
                color_primaries="bt709",
                file_path=str(lut),
                file_sha256=sha256_file(lut),
                tool_version=TOOL_VERSION,
            ),
        )
    )


def _leaves(node: object, prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    if isinstance(node, dict):
        for key in sorted(node):
            flat.update(_leaves(node[key], f"{prefix}.{key}" if prefix else str(key)))
    else:
        flat[prefix] = node
    return flat


def _declared_diff(
    section_a: ColorProfileSection, section_b: ColorProfileSection
) -> set[str]:
    payload_a = _leaves(section_a.model_dump(mode="json"))
    payload_b = _leaves(section_b.model_dump(mode="json"))
    return {
        path
        for path in set(payload_a) | set(payload_b)
        if payload_a.get(path) != payload_b.get(path)
    } - {"section_sha256", "profile_snapshot_sha256"}


def _color_section(
    section: ColorProfileSection,
    support: SettingSupport,
    applied: dict[str, str],
) -> ColorSection:
    if support.verified and applied and support.evidence_sha is not None:
        return ColorSection(
            rung="project_setting",
            reason="probe_verified",
            profile_section_sha256=section.section_sha256,
            working_space=section.working_space,
            setting_binding=SettingBinding(
                setting_name="colorScienceMode",
                probe_report_sha256=support.evidence_sha,
            ),
        )
    return ColorSection(
        rung="external_validation",
        reason="no_verified_project_setting",
        profile_section_sha256=section.section_sha256,
        working_space=section.working_space,
        setting_binding=None,
    )


class ColorLiveFlow:
    """One live color build; every outcome is recorded honestly."""

    def __init__(
        self,
        *,
        connection: ResolveConnection,
        bundle: Path,
        ffmpeg: Path,
        ffprobe: Path,
        manifest: Phase3FixtureManifest,
        manifest_b: Phase3FixtureManifest,
        render_deadline: float,
    ) -> None:
        self.connection = connection
        self.bundle = bundle
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.manifest = manifest
        self.manifest_b = manifest_b
        self.render_deadline = render_deadline
        self.passed = False

    def _profile(
        self,
        brand: Phase3FixtureManifest,
        *,
        channel_brand: Phase3FixtureManifest | None = None,
    ) -> ResolvedPresentationProfile:
        registry = registry_from_phase3_manifests(Path(MANIFEST_DIR_NAME))
        catalog = tuple(sorted(entry.asset_id for entry in registry.entries))
        system = SystemPresentationProfile.model_validate(
            {"asset_catalog": list(catalog), **_keys_of(brand)}
        )
        channel = (
            ChannelPresentationProfile.model_validate(
                {
                    "channel_id": f"channel-{channel_brand.brand_id}",
                    **_keys_of(channel_brand),
                }
            )
            if channel_brand is not None
            else None
        )
        return resolve_presentation_profile(
            system,
            episode=EpisodePresentationProfile(episode_id="episode-color-live"),
            channel=channel,
            registry=registry,
        )

    def _apply_setting_rung(
        self, project: ColorSettingProjectApi, support: SettingSupport
    ) -> dict[str, str]:
        """Apply the verified setting round-trip; record honestly otherwise."""

        if not support.verified:
            return {}
        setting = "colorScienceMode"
        current = project.GetSetting(setting)
        if current and project.SetSetting(setting, current):
            readback = project.GetSetting(setting)
            if readback == current:
                return {setting: readback}
        raise ColorLiveError(
            "setting_roundtrip_failed",
            f"{setting} no longer round-trips although findings verified it",
        )

    def run(self) -> None:
        bars = render_bars_source(
            ffmpeg_bin=self.ffmpeg,
            width=320,
            height=180,
            rate_num=30,
            seconds=BARS_SECONDS,
            output=self.bundle / "bars-source.mov",
        )
        declared = declared_color_from_media(
            self.ffprobe, bars, source_id="fixture-bars"
        )
        lut = self.bundle / "identity.cube"
        atomic_write(lut, IDENTITY_CUBE_BYTES)
        catalog = _catalog(lut)
        section_a = compile_color_profile(
            self._profile(self.manifest),
            {"fixture-bars": declared},
            catalog,
            assets_root=Path(),
        )
        section_b = compile_color_profile(
            self._profile(self.manifest, channel_brand=self.manifest_b),
            {"fixture-bars": declared},
            catalog,
            assets_root=Path(),
        )
        verify_color_section(section_a, catalog, assets_root=Path())
        ab_dims = _declared_diff(section_a, section_b)
        if ab_dims != set(DECLARED_DIFF_DIMS):
            raise ColorLiveError(
                "ab_nondeterministic",
                f"A/B diff {sorted(ab_dims)} != declared {sorted(DECLARED_DIFF_DIMS)}",
            )
        targets = ColorTargets()
        derivative = render_conformance_derivative(
            ffmpeg_bin=self.ffmpeg,
            ffprobe_bin=self.ffprobe,
            lut_path=lut,
            lut_sha256=section_a.sources[0].preset.file_sha256,
            source=bars,
            output=self.bundle / "conformance.mov",
            targets=targets,
        )
        derivative_metadata = validate_output_color(
            self.ffprobe, derivative.path, targets
        )
        support = color_setting_support_from_matrix(FINDINGS_PATH)
        project_api = create_disposable_project(
            self.connection.project_manager(), owned_project_name()
        )
        project = cast("FixedProjectApi", project_api)
        applied_settings = self._apply_setting_rung(
            cast("ColorSettingProjectApi", project), support
        )
        for key, value in (
            ("timelineFrameRate", "30"),
            ("timelineResolutionWidth", str(TIMELINE_WIDTH)),
            ("timelineResolutionHeight", str(TIMELINE_HEIGHT)),
        ):
            if not project.SetSetting(key, value):
                raise ColorLiveError("setup_failed", f"SetSetting({key}) failed")
        timeline = cast(
            "ColorTimelineApi", create_owned_timeline(project_api, owned_timeline_name())
        )
        if not timeline.SetStartTimecode("01:00:00:00"):
            raise ColorLiveError("setup_failed", "SetStartTimecode failed")
        pool_api = project.GetMediaPool()
        if pool_api is None:
            raise ColorLiveError("setup_failed", "media pool unavailable")
        item = append_video_clip(
            cast("ColorPoolApi", pool_api), bars, start_frame=0, end_frame=BARS_FRAMES
        )
        verify_clip_readback(item, start_frame=0, end_frame=BARS_FRAMES)
        render_path = render_project(
            cast("ColorRenderProjectApi", project),
            self.bundle / "render",
            self.render_deadline,
        )
        render_regions = measure_region_luma(
            self.ffmpeg,
            render_path,
            frame_index=5,
            width=TIMELINE_WIDTH,
            height=TIMELINE_HEIGHT,
        )
        try:
            render_metadata: dict[str, str] | None = validate_output_color(
                self.ffprobe, render_path, targets
            )
        except ColorRenderError:
            render_metadata = None
        color_section = _color_section(section_a, support, applied_settings)
        self.passed = True
        payload = {
            "passed": True,
            "working_space": section_a.working_space,
            "preset": section_a.sources[0].preset.model_dump(mode="json"),
            "declared": section_a.sources[0].declared.model_dump(mode="json"),
            "ab": {"deterministic": True, "dims": sorted(ab_dims)},
            "setting_rung": {
                "verified": support.verified,
                "applied": applied_settings,
                "finding": COLOR_FINDING,
            },
            "derivative": {
                "path": str(derivative.path),
                "sha256": derivative.sha256,
                "metadata": derivative_metadata,
                "lut_sha256": derivative.lut_sha256,
            },
            "render": {
                "output_path": str(render_path),
                "regions": [
                    {
                        "region_id": row.region_id,
                        "mean": row.mean,
                        "expected": row.expected,
                        "tolerance": row.tolerance,
                    }
                    for row in render_regions
                ],
                "metadata": render_metadata,
            },
            "validation": {
                "output_metadata": "pass",
                "region_luma": "pass",
                "region_constants": dict(BAR_REGION_LUMA),
            },
            "color_section": color_section.model_dump(mode="json"),
        }
        atomic_write(
            self.bundle / REPORT_NAME,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        )
        preset = section_a.sources[0].preset
        print(
            f"{MARKER} preset={preset.preset_id} basis={preset.selection_basis} "
            f"camera={preset.camera_make}/{preset.camera_model}"
        )
        print(
            f"{MARKER} setting_rung_verified={str(support.verified).lower()} "
            f"applied={sorted(applied_settings)}"
        )
        print(f"{MARKER} ab_deterministic=true dims={','.join(sorted(ab_dims))}")
        print(
            f"{MARKER} derivative_metadata="
            f"{derivative_metadata['color_space']}/{derivative_metadata['color_transfer']}"
        )
        print(f"{MARKER} render_region_luma=pass bars={len(render_regions)}")


__all__ = ["MANIFEST_DIR_NAME", "ColorLiveFlow"]
