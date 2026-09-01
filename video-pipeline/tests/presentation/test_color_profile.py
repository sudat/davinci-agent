"""Todo-60 acceptance: Project Color Management + camera-preset application.

Offline: the color profile section compiles the profile's project color
settings (working space) plus the camera preset SELECTED from the source
manifest's DECLARED camera/color metadata — never guessed — with exact
preset/LUT file hashes and tool version. Unknown camera, missing color
metadata, mismatched HDR, absent/changed LUT bytes, and any per-shot
model-driven grade request are TYPED blocking/human refusals. The external
render-side validation applies the equivalent conversion through the frozen
ffmpeg filter set (checked honestly: scale/colorspace present, zscale not)
and validates output color metadata plus measured bar-region luma from
decoded bytes against DECLARED fixture constants.

Live: the project color settings probe records the honest SetSetting/
GetSetting round-trip verdict, and the end-to-end build applies the
project-level settings where live-readable, renders through the official
Deliver API, and verifies the rendered output's color metadata and region
luminance from the RENDERED output bytes. A/B brand profiles select
deterministically different sections differing only in the declared color
dims (the hex trio); the working space and the source-selected preset are
invariants.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from services.contracts.primitives import Producer, RationalFrameRate
from services.fixtures.manifest_phase3 import Phase3FixtureManifest
from services.foundation_io import atomic_write, sha256_file
from services.ingest.models import (
    HdrSignaling,
    SourceManifest,
    VideoStreamRecord,
)
from services.presentation.asset_registry import registry_from_phase3_manifests
from services.presentation.color_models import (
    CameraPresetCatalog,
    CameraPresetEntry,
    ColorProfileSection,
    ColorSection,
    ColorTargets,
    DeclaredCameraColor,
    SettingBinding,
)
from services.presentation.color_profile import (
    COLOR_FINDING,
    ColorProfileError,
    color_setting_support_from_matrix,
    compile_color_profile,
    declared_color_from_manifest,
    verify_color_section,
)
from services.presentation.color_render import (
    BAR_REGION_LUMA,
    IDENTITY_CUBE_BYTES,
    ColorRenderError,
    available_color_filters,
    measure_region_luma,
    render_conformance_derivative,
    require_conversion_filters,
    validate_output_color,
)
from services.presentation.models import (
    EpisodePresentationProfile,
    ResolvedPresentationProfile,
)
from services.presentation.profiles import resolve_presentation_profile
from services.preview.tools import load_pinned_tools
from services.resolve_adapter.color_section import attach_color_section
from services.resolve_adapter.errors import PackageCompileError
from services.resolve_adapter.package import (
    PackageCompileRequest,
    compile_resolve_package,
)
from services.resolve_bridge.color_setting_probe import derive_setting_findings
from services.resolve_bridge.color_setting_probe_models import (
    ColorSettingProbeReport,
    SettingProbe,
)
from services.resolve_bridge.connection import connect
from services.resolve_bridge.lifecycle import PROJECT_PREFIX
from services.resolve_bridge.readiness import load_host_report
from services.resolve_bridge.title_probe import append_matrix_findings
from services.spike.gate_models import ApiFindingEntry, EvidenceRef
from tests.presentation.test_manifest import _channel, _system
from tests.resolve_adapter.support import (
    declared_media,
    ir_for,
    load_p2_manifest,
    lock_sha256,
    phase2_lock,
)

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-3")
ASSETS_ROOT = Path()
RATE = RationalFrameRate(num=30, den=1)
SEAL_FIELDS = frozenset({"section_sha256", "profile_snapshot_sha256"})
SHA = "a" * 64
PROBE_SHA = "b" * 64
FIXTURE_MAKE = "FIXTURECAM"
FIXTURE_MODEL = "FC-1"
TOOL_VERSION = "ffmpeg-7.1.1"


# ------------------------------------------------------------------ helpers --


def _registry_and_catalog(root: Path) -> tuple[Path, CameraPresetCatalog]:
    registry_from_phase3_manifests(MANIFEST_DIR)
    lut = root / "identity.cube"
    lut.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(lut, IDENTITY_CUBE_BYTES)

    catalog_model = CameraPresetCatalog(
        presets=(
            CameraPresetEntry(
                preset_id="fixturecam-fc1-rec709",
                kind="lut",
                camera_make=FIXTURE_MAKE,
                camera_model=FIXTURE_MODEL,
                color_space="bt709",
                color_transfer="bt709",
                color_primaries="bt709",
                file_path=str(lut),
                file_sha256=sha256_file(lut),
                tool_version=TOOL_VERSION,
            ),
            CameraPresetEntry(
                preset_id="generic-bt709-sdr",
                kind="lut",
                camera_make=None,
                camera_model=None,
                color_space="bt709",
                color_transfer="bt709",
                color_primaries="bt709",
                file_path=str(lut),
                file_sha256=sha256_file(lut),
                tool_version=TOOL_VERSION,
            ),
        )
    )
    return lut, catalog_model


@pytest.fixture(scope="module")
def lut_and_catalog(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, CameraPresetCatalog]:
    return _registry_and_catalog(tmp_path_factory.mktemp("todo60-lut"))


@pytest.fixture(scope="module")
def brand_a() -> Phase3FixtureManifest:
    return Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / "p3-brand-a.json").read_bytes()
    )


@pytest.fixture(scope="module")
def brand_b() -> Phase3FixtureManifest:
    return Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / "p3-brand-b.json").read_bytes()
    )


@pytest.fixture(scope="module")
def profile_a(
    brand_a: Phase3FixtureManifest,
) -> ResolvedPresentationProfile:

    registry = registry_from_phase3_manifests(MANIFEST_DIR)
    ids = tuple(sorted(entry.asset_id for entry in registry.entries))
    return resolve_presentation_profile(
        _system(brand_a, ids),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        registry=registry,
    )


@pytest.fixture(scope="module")
def profile_b(
    brand_a: Phase3FixtureManifest,
    brand_b: Phase3FixtureManifest,
) -> ResolvedPresentationProfile:

    registry = registry_from_phase3_manifests(MANIFEST_DIR)
    ids = tuple(sorted(entry.asset_id for entry in registry.entries))
    return resolve_presentation_profile(
        _system(brand_a, ids),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        channel=_channel(brand_b),
        registry=registry,
    )


def _declared(
    *,
    make: str | None = FIXTURE_MAKE,
    model: str | None = FIXTURE_MODEL,
    transfer: str = "bt709",
) -> DeclaredCameraColor:
    return DeclaredCameraColor(
        source_id="fixture-bars",
        camera_make=make,
        camera_model=model,
        color_space="bt709",
        color_transfer=transfer,
        color_primaries="bt709",
    )


def _section(
    profile: ResolvedPresentationProfile,
    catalog: CameraPresetCatalog,
    *,
    declared: DeclaredCameraColor | None = None,
    per_shot_grade_request: str | None = None,
) -> ColorProfileSection:
    return compile_color_profile(
        profile,
        {"fixture-bars": declared if declared is not None else _declared()},
        catalog,
        assets_root=ASSETS_ROOT,
        per_shot_grade_request=per_shot_grade_request,
    )


@pytest.fixture(scope="module")
def section_a(
    profile_a: ResolvedPresentationProfile,
    lut_and_catalog: tuple[Path, CameraPresetCatalog],
) -> ColorProfileSection:
    _, catalog = lut_and_catalog
    return _section(profile_a, catalog)


@pytest.fixture(scope="module")
def section_b(
    profile_b: ResolvedPresentationProfile,
    lut_and_catalog: tuple[Path, CameraPresetCatalog],
) -> ColorProfileSection:
    _, catalog = lut_and_catalog
    return _section(profile_b, catalog)


def _flatten(node: object, prefix: str = "") -> dict[str, object]:
    leaves: dict[str, object] = {}
    if isinstance(node, dict):
        for key in sorted(node):
            leaves.update(_flatten(node[key], f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            leaves.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        leaves[prefix] = node
    return leaves


# ------------------------------------------------- selection + compile A/B ----


def test_compile_selects_preset_from_declared_camera_metadata(
    profile_a: ResolvedPresentationProfile,
    section_a: ColorProfileSection,
    lut_and_catalog: tuple[Path, CameraPresetCatalog],
) -> None:
    _, catalog = lut_and_catalog
    binding = section_a.sources[0]
    assert binding.source_id == "fixture-bars"
    assert binding.preset.preset_id == "fixturecam-fc1-rec709"
    assert binding.preset.selection_basis == "camera_make_model"
    assert binding.preset.camera_make == FIXTURE_MAKE
    assert binding.preset.camera_model == FIXTURE_MODEL
    entry = next(
        preset
        for preset in catalog.presets
        if preset.preset_id == binding.preset.preset_id
    )
    assert binding.preset.file_sha256 == entry.file_sha256
    assert binding.preset.tool_version == TOOL_VERSION
    assert section_a.working_space == "rec709"
    assert section_a.verify_hash() is True
    verify_color_section(section_a, catalog, assets_root=ASSETS_ROOT)

    colorimetry_only = _section(
        profile_a, catalog, declared=_declared(make=None, model=None)
    )
    assert colorimetry_only.sources[0].preset.selection_basis == "declared_colorimetry"
    assert colorimetry_only.sources[0].preset.preset_id == "generic-bt709-sdr"


def test_double_compile_is_byte_identical(
    profile_a: ResolvedPresentationProfile,
    lut_and_catalog: tuple[Path, CameraPresetCatalog],
) -> None:
    _, catalog = lut_and_catalog
    assert _section(profile_a, catalog).canonical_bytes() == _section(
        profile_a, catalog
    ).canonical_bytes()


def test_ab_sections_differ_only_in_declared_color_dims(
    section_a: ColorProfileSection, section_b: ColorProfileSection
) -> None:
    leaves_a = _flatten(section_a.model_dump(mode="json"))
    leaves_b = _flatten(section_b.model_dump(mode="json"))
    diffs = {
        path
        for path in set(leaves_a) | set(leaves_b)
        if leaves_a.get(path) != leaves_b.get(path)
    } - SEAL_FIELDS
    assert diffs == {"primary_hex", "accent_hex", "neutral_hex"}, diffs
    assert section_a.section_sha256 != section_b.section_sha256
    assert section_a.working_space == section_b.working_space == "rec709"
    assert section_a.sources[0].preset == section_b.sources[0].preset


# ------------------------------------------------------- typed fault paths ----


def _video_stream(
    *,
    color_space: str | None,
    transfer: str | None,
    primaries: str | None,
    hdr: HdrSignaling | None = None,
) -> VideoStreamRecord:
    return VideoStreamRecord(
        index=0,
        codec_type="video",
        codec_name="h264",
        time_base_num=1,
        time_base_den=30,
        start_pts=0,
        duration_num=30,
        duration_den=1,
        r_frame_rate_num=30,
        r_frame_rate_den=1,
        avg_frame_rate_num=30,
        avg_frame_rate_den=1,
        width=1920,
        height=1080,
        pix_fmt="yuv420p",
        color_space=color_space,
        color_transfer=transfer,
        color_primaries=primaries,
        hdr=hdr or HdrSignaling(
            dolby_vision_rpu=False,
            hdr10_mastering_display=False,
            smpte2094=False,
        ),
    )


def _manifest(stream: VideoStreamRecord) -> SourceManifest:

    return SourceManifest.model_validate(
        {
            "schema_version": "source-manifest-v1",
            "artifact_type": "source-manifest",
            "artifact_id": "fixture-bars",
            "content_hash": SHA,
            "producer": Producer(name="test", version="1"),
            "file": {
                "path": "bars.mov",
                "size_bytes": 10,
                "sha256": SHA,
            },
            "container": {
                "format_name": "mov",
                "nb_streams": 1,
                "duration_num": 1,
                "duration_den": 1,
            },
            "streams": (stream.model_dump(mode="json"),),
            "monotonicity": (),
            "vfr_evidence": None,
            "edit_source_recipe": {
                "recipe_id": "r",
                "recipe_source": "t",
                "args_sha256": SHA,
            },
            "eligibility": {"verdict": "supported", "reasons": ()},
            "probe": {
                "ffprobe_path": "ffprobe",
                "ffprobe_sha256": SHA,
                "arguments": ("-v", "error"),
            },
        }
    )


def test_missing_color_metadata_is_typed(
    profile_a: ResolvedPresentationProfile,
    lut_and_catalog: tuple[Path, CameraPresetCatalog],
) -> None:
    manifest = _manifest(_video_stream(color_space=None, transfer=None, primaries=None))
    with pytest.raises(ColorProfileError) as raised:
        declared_color_from_manifest(manifest, camera_make=None, camera_model=None)
    assert raised.value.code == "missing_color_metadata"

    declared = declared_color_from_manifest(
        _manifest(
            _video_stream(
                color_space="bt709", transfer="bt709", primaries="bt709"
            )
        ),
        camera_make=FIXTURE_MAKE,
        camera_model=FIXTURE_MODEL,
    )
    assert declared.color_space == "bt709"
    with pytest.raises(ValidationError):
        DeclaredCameraColor(
            source_id="x",
            camera_make=None,
            camera_model=None,
            color_space="",
            color_transfer="bt709",
            color_primaries="bt709",
        )


def test_mismatched_hdr_metadata_is_typed(
    profile_a: ResolvedPresentationProfile,
    lut_and_catalog: tuple[Path, CameraPresetCatalog],
) -> None:
    _, catalog = lut_and_catalog
    with pytest.raises(ColorProfileError) as raised:
        _section(profile_a, catalog, declared=_declared(transfer="smpte2084"))
    assert raised.value.code == "hdr_unsupported"

    with pytest.raises(ColorProfileError) as raised:
        _section(profile_a, catalog, declared=_declared(transfer="arib-std-b67"))
    assert raised.value.code == "hdr_unsupported"

    hdr10 = _manifest(
        _video_stream(
            color_space="bt2020nc",
            transfer="smpte2084",
            primaries="bt2020",
            hdr=HdrSignaling(
                dolby_vision_rpu=False,
                hdr10_mastering_display=True,
                smpte2094=False,
            ),
        )
    )
    with pytest.raises(ColorProfileError) as raised:
        declared_color_from_manifest(hdr10, camera_make=None, camera_model=None)
    assert raised.value.code == "hdr_unsupported"


def test_absent_or_changed_lut_is_typed(
    profile_a: ResolvedPresentationProfile,
    lut_and_catalog: tuple[Path, CameraPresetCatalog],
) -> None:
    lut, catalog = lut_and_catalog
    moved = lut.with_suffix(".moved")
    lut.rename(moved)
    try:
        with pytest.raises(ColorProfileError) as raised:
            _section(profile_a, catalog)
        assert raised.value.code == "lut_missing"
    finally:
        moved.rename(lut)

    tampered = CameraPresetCatalog.model_validate(
        catalog.model_dump(mode="json")
    ).model_copy(
        update={
            "presets": tuple(
                preset.model_copy(update={"file_sha256": "c" * 64})
                for preset in catalog.presets
            )
        }
    )
    with pytest.raises(ColorProfileError) as raised:
        _section(profile_a, tampered)
    assert raised.value.code == "lut_changed"


def test_unknown_camera_is_blocking_human(
    profile_a: ResolvedPresentationProfile,
    lut_and_catalog: tuple[Path, CameraPresetCatalog],
) -> None:
    _, catalog = lut_and_catalog
    with pytest.raises(ColorProfileError) as raised:
        _section(
            profile_a, catalog, declared=_declared(make="OTHERCAM", model="X-9")
        )
    assert raised.value.code == "unknown_camera"

    with pytest.raises(ColorProfileError) as raised:
        _section(
            profile_a,
            catalog,
            declared=_declared(make=None, model=None, transfer="bt2020-10"),
        )
    assert raised.value.code == "unknown_camera"


def test_per_shot_grade_request_is_refused(
    profile_a: ResolvedPresentationProfile,
    lut_and_catalog: tuple[Path, CameraPresetCatalog],
) -> None:
    _, catalog = lut_and_catalog
    with pytest.raises(ColorProfileError) as raised:
        _section(
            profile_a,
            catalog,
            per_shot_grade_request="scene-12: warmer, +saturation per shot",
        )
    assert raised.value.code == "per_shot_grade_refused"


def test_verify_color_section_detects_drift(
    section_a: ColorProfileSection,
    lut_and_catalog: tuple[Path, CameraPresetCatalog],
) -> None:
    lut, catalog = lut_and_catalog
    unsealed = section_a.model_copy(update={"section_sha256": "0" * 64})
    with pytest.raises(ColorProfileError) as raised:
        verify_color_section(unsealed, catalog, assets_root=ASSETS_ROOT)
    assert raised.value.code == "section_seal_invalid"

    empty = CameraPresetCatalog(
        presets=(
            CameraPresetEntry.model_validate(
                {
                    **catalog.presets[0].model_dump(mode="json"),
                    "preset_id": "not-registered",
                }
            ),
        )
    )
    with pytest.raises(ColorProfileError) as raised:
        verify_color_section(section_a, empty, assets_root=ASSETS_ROOT)
    assert raised.value.code == "preset_unregistered"

    drifted = Path(lut)
    drifted.write_bytes(drifted.read_bytes() + b"\n# stale\n")
    try:
        with pytest.raises(ColorProfileError) as raised:
            verify_color_section(section_a, catalog, assets_root=ASSETS_ROOT)
        assert raised.value.code == "lut_changed"
    finally:
        drifted.write_bytes(IDENTITY_CUBE_BYTES)


def test_malformed_color_sections_are_refused(
    section_a: ColorProfileSection,
) -> None:
    with pytest.raises(ValidationError):
        ColorProfileSection.model_validate(
            section_a.model_copy(update={"sources": ()}).model_dump()
        )
    with pytest.raises(ValidationError):
        ColorProfileSection.model_validate(
            section_a.model_copy(
                update={"sources": (section_a.sources[0], section_a.sources[0])}
            ).model_dump()
        )
    with pytest.raises(ValidationError):
        ColorSection(
            rung="project_setting",
            reason="probe verified",
            profile_section_sha256=section_a.section_sha256,
            working_space="rec709",
            setting_binding=None,
        )
    with pytest.raises(ValidationError):
        CameraPresetCatalog(
            presets=(
                CameraPresetEntry.model_validate(
                    {
                        **section_a.sources[0].preset.model_dump(mode="json"),
                        "selection_basis": "declared_colorimetry",
                        "camera_make": "broken",
                    }
                ),
                CameraPresetEntry.model_validate(
                    {
                        **section_a.sources[0].preset.model_dump(mode="json"),
                        "preset_id": "second",
                    }
                ),
            )
        )


def test_setting_support_from_matrix_fails_closed(tmp_path: Path) -> None:
    assert (
        color_setting_support_from_matrix(Path("/nonexistent.json")).verified is False
    )

    def _finding(name: str, *, verified: bool) -> ApiFindingEntry:
        return ApiFindingEntry(
            finding=name,
            api_available=True,
            live_verified=verified,
            evidence_refs=(
                EvidenceRef(path="color-setting-probe/report.json", sha256=PROBE_SHA),
            ),
            limitations="probe limits",
        )

    partial = tmp_path / "partial.json"
    append_matrix_findings(partial, (_finding("some-other-finding", verified=True),))
    assert color_setting_support_from_matrix(partial).verified is False

    verified = tmp_path / "verified.json"
    append_matrix_findings(verified, (_finding(COLOR_FINDING, verified=True),))
    support = color_setting_support_from_matrix(verified)
    assert support.verified is True
    assert support.evidence_sha == PROBE_SHA

    downgraded = tmp_path / "down.json"
    append_matrix_findings(downgraded, (_finding(COLOR_FINDING, verified=False),))
    assert color_setting_support_from_matrix(downgraded).verified is False


# --------------------------------------------------- external render path ----


@dataclass(frozen=True, slots=True)
class Pinned:
    ffmpeg: Path
    ffprobe: Path


@pytest.fixture(scope="module")
def tools() -> Pinned:
    try:
        pinned = load_pinned_tools(Path("config/toolchains/phase-0c-v2.json"))
    except Exception as error:  # noqa: BLE001 (skip on any toolchain failure)
        pytest.skip(f"pinned toolchain unavailable: {error}")
    return Pinned(ffmpeg=pinned.ffmpeg, ffprobe=pinned.ffprobe)


@pytest.fixture(scope="module")
def bars_source(tools: Pinned, tmp_path_factory: pytest.TempPathFactory) -> Path:
    from services.presentation.color_live_media import (  # noqa: PLC0415 (fixture-local, keeps module import surface lean)
        render_bars_source,
    )

    out = tmp_path_factory.mktemp("todo60-bars") / "bars.mov"
    return render_bars_source(
        ffmpeg_bin=tools.ffmpeg,
        width=320,
        height=180,
        rate_num=30,
        seconds=1,
        output=out,
    )


def test_filter_availability_is_reported_honestly(tools: Pinned) -> None:
    filters = available_color_filters(tools.ffmpeg)
    assert "scale" in filters
    assert "colorspace" in filters
    assert "zscale" not in filters  # the frozen build ships no libzimg
    require_conversion_filters(filters)


def test_unsupported_hdr_conversion_is_typed(tools: Pinned) -> None:
    with pytest.raises(ColorRenderError) as raised:
        require_conversion_filters(
            frozenset(), source_transfer="smpte2084", target_transfer="bt709"
        )
    assert raised.value.code == "hdr_conversion_unsupported"
    with pytest.raises(ColorRenderError) as raised:
        require_conversion_filters(
            available_color_filters(tools.ffmpeg),
            source_transfer="arib-std-b67",
        )
    assert raised.value.code == "hdr_conversion_unsupported"
    with pytest.raises(ColorRenderError) as raised:
        require_conversion_filters(frozenset({"volume"}))
    assert raised.value.code == "conversion_unavailable"


def test_conformance_derivative_validates_metadata_and_luma(
    tools: Pinned,
    section_a: ColorProfileSection,
    lut_and_catalog: tuple[Path, CameraPresetCatalog],
    bars_source: Path,
    tmp_path: Path,
) -> None:
    targets = ColorTargets()
    derivative = render_conformance_derivative(
        ffmpeg_bin=tools.ffmpeg,
        ffprobe_bin=tools.ffprobe,
        lut_path=Path(section_a.sources[0].preset.file_path),
        lut_sha256=section_a.sources[0].preset.file_sha256,
        source=bars_source,
        output=tmp_path / "conformance.mov",
        targets=targets,
    )
    probed = validate_output_color(tools.ffprobe, derivative.path, targets)
    assert probed == {"color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709"}
    assert derivative.argv[0] == str(tools.ffmpeg)

    regions = measure_region_luma(
        tools.ffmpeg,
        derivative.path,
        frame_index=5,
        width=320,
        height=180,
    )
    declared_luma = dict(BAR_REGION_LUMA)
    for row in regions:
        assert row.expected == declared_luma[row.region_id]
        assert abs(row.mean - row.expected) <= row.tolerance, row.region_id

    source_regions = measure_region_luma(
        tools.ffmpeg, bars_source, frame_index=5, width=320, height=180
    )
    for row, source_row in zip(regions, source_regions, strict=True):
        assert abs(row.mean - source_row.mean) <= 4, row.region_id


def test_ab_derivative_metadata_is_invariant(
    tools: Pinned,
    section_a: ColorProfileSection,
    section_b: ColorProfileSection,
    bars_source: Path,
    tmp_path: Path,
) -> None:
    targets = ColorTargets()
    derivative_a = render_conformance_derivative(
        ffmpeg_bin=tools.ffmpeg,
        ffprobe_bin=tools.ffprobe,
        lut_path=Path(section_a.sources[0].preset.file_path),
        lut_sha256=section_a.sources[0].preset.file_sha256,
        source=bars_source,
        output=tmp_path / "conf-a.mov",
        targets=targets,
    )
    derivative_b = render_conformance_derivative(
        ffmpeg_bin=tools.ffmpeg,
        ffprobe_bin=tools.ffprobe,
        lut_path=Path(section_b.sources[0].preset.file_path),
        lut_sha256=section_b.sources[0].preset.file_sha256,
        source=bars_source,
        output=tmp_path / "conf-b.mov",
        targets=targets,
    )
    probed_a = validate_output_color(tools.ffprobe, derivative_a.path, targets)
    probed_b = validate_output_color(tools.ffprobe, derivative_b.path, targets)
    assert probed_a == probed_b
    assert derivative_a.argv[:-1] == derivative_b.argv[:-1]
    assert derivative_a.argv[-1] != derivative_b.argv[-1]


def test_output_metadata_mismatch_is_typed(
    tools: Pinned, bars_source: Path, tmp_path: Path
) -> None:
    wrong = ColorTargets().model_copy(update={"color_space": "smpte170m"})
    with pytest.raises(ColorRenderError) as raised:
        validate_output_color(
            tools.ffprobe,
            bars_source,
            wrong,
        )
    assert raised.value.code == "output_metadata_mismatch"

    with pytest.raises(ColorRenderError) as raised:
        measure_region_luma(
            tools.ffmpeg,
            bars_source,
            frame_index=5,
            width=320,
            height=180,
            expectations={"gray75": (40, 6)},
        )
    assert raised.value.code == "luminance_out_of_tolerance"

    with pytest.raises(ColorRenderError) as raised:
        render_conformance_derivative(
            ffmpeg_bin=tools.ffmpeg,
            ffprobe_bin=tools.ffprobe,
            lut_path=tmp_path / "absent.cube",
            lut_sha256="c" * 64,
            source=bars_source,
            output=tmp_path / "none.mov",
            targets=ColorTargets(),
        )
    assert raised.value.code == "lut_hash_drift"


# --------------------------------------------------------- package wiring ----


def test_package_carries_color_section(
    section_a: ColorProfileSection, tmp_path: Path
) -> None:
    report = SettingBinding(
        setting_name="colorScienceMode", probe_report_sha256=PROBE_SHA
    )
    color = ColorSection(
        rung="project_setting",
        reason="probe_verified",
        profile_section_sha256=section_a.section_sha256,
        working_space="rec709",
        setting_binding=report,
    )
    external = ColorSection(
        rung="external_validation",
        reason="no_verified_project_setting",
        profile_section_sha256=section_a.section_sha256,
        working_space="rec709",
        setting_binding=None,
    )
    manifest = load_p2_manifest("p2-stale-capability")
    ir = ir_for(manifest)
    request = PackageCompileRequest(
        ir=ir,
        lock=phase2_lock(),
        lock_sha256=lock_sha256(),
        declared_media=declared_media(manifest),
        artifact_id="resolve-package-color-test",
    )
    legacy = compile_resolve_package(request)
    assert legacy.color is None
    with_color = compile_resolve_package(
        dataclasses.replace(request, color_section=color)
    )
    assert with_color.color is not None
    assert with_color.color.rung == "project_setting"
    assert with_color.content_hash != legacy.content_hash
    assert ColorSection.model_validate(
        with_color.color.model_dump(mode="json")
    ).setting_binding is not None

    attached = attach_color_section(color, section_a)
    assert attached.rung == color.rung
    with pytest.raises(PackageCompileError) as raised:
        attach_color_section(
            external.model_copy(
                update={"profile_section_sha256": "c" * 64}
            ),
            section_a,
        )
    assert raised.value.code == "color-section-drift"


# ------------------------------------------------------------ probe honest ----


def test_setting_probe_findings_derive_honestly() -> None:

    unreadable = ColorSettingProbeReport(
        schema_version="color-setting-probe-report-v1",
        binding="21.0.4.5",
        settings=(
            SettingProbe(setting="colorScienceMode", value="", set_roundtrip=False),
        ),
        steps=(),
    )
    findings = derive_setting_findings(unreadable, report_sha=PROBE_SHA)
    setting = next(f for f in findings if f.finding == COLOR_FINDING)
    assert setting.api_available is True
    assert setting.live_verified is False

    verified = ColorSettingProbeReport(
        schema_version="color-setting-probe-report-v1",
        binding="21.0.4.5",
        settings=(
            SettingProbe(setting="colorScienceMode", value="4", set_roundtrip=True),
        ),
        steps=(),
    )
    findings = derive_setting_findings(verified, report_sha=PROBE_SHA)
    setting = next(f for f in findings if f.finding == COLOR_FINDING)
    assert setting.live_verified is True


# ------------------------------------------------------------------- live ----


ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
EVIDENCE_NAME = "resolve"


def live_evidence_root() -> Path:
    return ATTEMPT / EVIDENCE_NAME


def _report_path(config: pytest.Config) -> Path | None:
    override = os.environ.get("RESOLVE_HOST_REPORT")
    if override:
        return Path(override)
    evidence = config.getoption("--resolve-evidence")
    if evidence:
        candidate = Path(str(evidence)).resolve().parent / "resolve-host.json"
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class LiveEnv:
    report: Path
    evidence: Path
    ffmpeg: Path
    ffprobe: Path


@pytest.fixture(scope="session")
def live_env(request: pytest.FixtureRequest) -> Iterator[LiveEnv]:
    report_path = _report_path(request.config)
    if report_path is None:
        pytest.skip("resolve host report not found: pass --resolve-evidence")
    evidence = Path(str(request.config.getoption("--resolve-evidence"))) if (
        request.config.getoption("--resolve-evidence")
    ) else None
    if evidence is None:
        pytest.skip("live requires --resolve-evidence")
    if request.config.getoption("--exclusive-resolve-lease"):
        lock_path = evidence / "resolve-lease.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pytest.skip("exclusive resolve lease held by another session")
        print(f"exclusive resolve lease acquired: {lock_path}")
        try:
            yield _live_env(report_path, evidence)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        return
    yield _live_env(report_path, evidence)


def _live_env(report_path: Path, evidence: Path) -> LiveEnv:
    ffmpeg = evidence.parent / "bootstrap/ffmpeg-7.1.1/bin/ffmpeg"
    ffprobe = evidence.parent / "bootstrap/ffmpeg-7.1.1/bin/ffprobe"
    missing = [str(path) for path in (ffmpeg, ffprobe) if not Path(path).is_file()]
    if missing:
        pytest.skip(f"live pinned tools missing: {missing}")
    return LiveEnv(report=report_path, evidence=evidence, ffmpeg=ffmpeg, ffprobe=ffprobe)


@dataclass(frozen=True, slots=True)
class ProbeRun:
    stdout: str
    report: dict[str, object]


@pytest.fixture(scope="session")
def setting_probe_run(live_env: LiveEnv) -> ProbeRun:
    bundle = live_env.evidence / "color-setting-probe"
    bundle.mkdir(parents=True, exist_ok=True)
    argv = (
        sys.executable,
        "-m",
        "services.resolve_bridge.color_setting_probe",
        "--report",
        str(live_env.report),
        "--evidence",
        str(live_env.evidence),
        "--timeout",
        "240",
    )
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(
        (bundle / "color-setting-probe-report.json").read_bytes()
    )
    return ProbeRun(stdout=result.stdout, report=report)


@pytest.fixture(scope="session")
def color_live_run(
    live_env: LiveEnv, setting_probe_run: ProbeRun
) -> subprocess.CompletedProcess[str]:
    bundle = live_env.evidence / "color-live"
    bundle.mkdir(parents=True, exist_ok=True)
    argv = (
        sys.executable,
        "-m",
        "services.presentation.color_live",
        "--report",
        str(live_env.report),
        "--evidence",
        str(live_env.evidence),
        "--ffmpeg",
        str(live_env.ffmpeg),
        "--ffprobe",
        str(live_env.ffprobe),
        "--timeout",
        "900",
    )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=1200)


@pytest.mark.resolve_live
def test_live_color_setting_probe_records_honest_capability(
    setting_probe_run: ProbeRun,
) -> None:
    assert "color-setting-probe:" in setting_probe_run.stdout
    report = setting_probe_run.report
    settings = cast("list[dict[str, object]]", report["settings"])
    assert settings, "the probe must record its candidate settings honestly"
    verified = any(bool(row["set_roundtrip"]) for row in settings)
    if verified:
        rows = [row for row in settings if bool(row["set_roundtrip"])]
        assert all(str(row["value"]) != "" for row in rows)
        findings = live_evidence_root() / "color-setting-findings.json"
        if findings.is_file():
            rows = json.loads(findings.read_bytes())
            findings_rows = cast("list[dict[str, object]]", rows["findings"])
            assert any(
                f_row["finding"] == COLOR_FINDING and f_row["live_verified"]
                for f_row in findings_rows
            )
    else:
        assert not any(
            bool(row["set_roundtrip"]) and str(row["value"]) == "" for row in settings
        )
    assert isinstance(report["steps"], list)


@pytest.mark.resolve_live
def test_live_color_end_to_end(color_live_run: subprocess.CompletedProcess[str]) -> None:
    result = color_live_run
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "color-live: PASS" in result.stdout
    assert "color-live: preset=fixturecam-fc1-rec709" in result.stdout
    report = json.loads(
        (live_evidence_root() / "color-live" / "color-live-report.json").read_bytes()
    )
    assert report["passed"] is True
    assert report["preset"]["preset_id"] == "fixturecam-fc1-rec709"
    assert report["preset"]["selection_basis"] == "camera_make_model"
    assert report["working_space"] == "rec709"
    ab = cast("dict[str, object]", report["ab"])
    assert ab["deterministic"] is True
    assert set(cast("list[str]", ab["dims"])) == {
        "primary_hex",
        "accent_hex",
        "neutral_hex",
    }
    derivative = cast("dict[str, object]", report["derivative"])
    assert derivative["metadata"] == {
        "color_space": "bt709",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
    }
    render = cast("dict[str, object]", report["render"])
    assert Path(str(render["output_path"])).is_file()
    regions = cast("list[dict[str, object]]", render["regions"])
    assert regions
    for row in regions:
        mean = cast("int", row["mean"])
        expected = cast("int", row["expected"])
        tolerance = cast("int", row["tolerance"])
        assert abs(mean - expected) <= tolerance
    validate = cast("dict[str, object]", report["validation"])
    assert validate["output_metadata"] == "pass"
    assert validate["region_luma"] == "pass"


@pytest.mark.resolve_live
def test_live_cleanup_leaves_no_owned_projects(live_env: LiveEnv) -> None:
    connection = connect(load_host_report(live_env.report))
    manager = connection.project_manager()
    owned = [
        name
        for name in manager.GetProjectListInCurrentFolder()
        if name.startswith(PROJECT_PREFIX)
    ]
    assert owned == [], f"color-live leaked owned projects: {owned}"
