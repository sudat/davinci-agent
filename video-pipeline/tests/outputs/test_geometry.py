"""工程5 vertical variant: geometry, registry, and per-output seams (hermetic).

No ffmpeg, no Resolve, no network: geometry pinning, store-path scoping,
render/QC/preview/telop parameter flow, and the declared-orientation QC
rule are all asserted over fake evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from services.cli._v44_chapter_card_qa import canvas_for as qa_canvas_for
from services.cli._v44_chapter_card_qa import frame_bytes_for
from services.compile.subtitle_policy import SubtitleQcPolicy
from services.contracts.primitives import Producer
from services.creative_plan.quality_domains import ExecutionFactsV1
from services.episode_cockpit.models import RebuildRequestEntry
from services.fixtures.manifest import Phase0AFixtureManifest
from services.ingest import models as ingest_models
from services.ingest.models import (
    AudioStreamRecord,
    ContainerInfo,
    Eligibility,
    FileIdentity,
    RecipePointer,
    SourceManifest,
    StreamMonotonicity,
    VideoStreamRecord,
)
from services.mcp_execution.live_handlers.telop_style import canvas_size_for
from services.mcp_execution.plan_steps import render_native_steps
from services.mcp_execution.step_builders import CapabilityView
from services.outputs.geometry import (
    DEFAULT_OUTPUT_ID,
    LANDSCAPE_GEOMETRY,
    VERTICAL_GEOMETRY,
    OutputGeometryV1,
    OutputRegistryV1,
    approvals_relatives,
    chain_store_relatives,
    default_registry,
    geometry_for,
    load_registry,
    normalize_output_id,
    preview_relatives,
    preview_size_for,
    register_output,
    review_store_relatives,
    scale_px_for_output,
    subtitle_chars_for_output,
)
from services.presentation.chapter_card import canvas_size_for as chapter_canvas_for
from services.presentation.overlay_live_media import geo_for
from services.preview.render import preview_name_for
from services.qc.checks.orientation_checks import (
    ExpectedOrientation,
    OrientationCheckRequest,
    check_orientation,
    expected_orientation,
)
from services.qc.issue_factory import IssueFactory
from services.resolve_bridge.fixed_presentation import apply_project_settings
from services.resolve_bridge.fixed_presentation_models import (
    FfprobeFormat,
    FfprobeReport,
    FfprobeStream,
)
from services.toolchain.render_qc import (
    RenderGeometry,
    RenderPreset,
    RenderQcSection,
    RenderQcSmokeError,
    geometry_for_output,
    verify_render_geometry,
)
from tests.qc.support import clean_policy, preset


def test_geometries_are_pinned() -> None:
    assert (LANDSCAPE_GEOMETRY.width, LANDSCAPE_GEOMETRY.height) == (1920, 1080)
    assert LANDSCAPE_GEOMETRY.orientation == "landscape"
    assert (VERTICAL_GEOMETRY.width, VERTICAL_GEOMETRY.height) == (1080, 1920)
    assert VERTICAL_GEOMETRY.orientation == "portrait"
    assert geometry_for("landscape") == LANDSCAPE_GEOMETRY
    assert geometry_for("vertical") == VERTICAL_GEOMETRY


def test_geometry_rejects_wrong_dimensions() -> None:
    with pytest.raises(ValidationError):
        OutputGeometryV1(
            output_id="vertical", orientation="portrait", width=1920, height=1080
        )
    with pytest.raises(ValidationError):
        OutputGeometryV1(
            output_id="landscape", orientation="landscape", width=1080, height=1920
        )


def test_normalize_output_id_defaults_to_landscape() -> None:
    assert normalize_output_id(None) == "landscape"
    assert normalize_output_id("") == "landscape"
    assert normalize_output_id("landscape") == "landscape"
    assert normalize_output_id("vertical") == "vertical"
    assert DEFAULT_OUTPUT_ID == "landscape"
    with pytest.raises(ValueError, match="unknown output_id"):
        normalize_output_id("square")


def test_registry_defaults_to_landscape_only(tmp_path: Path) -> None:
    registry = load_registry(tmp_path)
    assert [output.output_id for output in registry.outputs] == ["landscape"]


def test_register_output_is_idempotent(tmp_path: Path) -> None:
    first, created = register_output(tmp_path, "vertical")
    assert created is True
    assert [output.output_id for output in first.outputs] == ["landscape", "vertical"]
    second, created_again = register_output(tmp_path, "vertical")
    assert created_again is False
    assert second == first
    assert load_registry(tmp_path) == first


def test_register_landscape_is_a_noop(tmp_path: Path) -> None:
    registry, created = register_output(tmp_path, "landscape")
    assert created is False
    assert registry == default_registry()


def test_registry_rejects_landscape_loss() -> None:
    with pytest.raises(ValidationError):
        OutputRegistryV1(schema_version="episode-outputs-v1", outputs=(VERTICAL_GEOMETRY,))


def test_review_store_paths_scope_per_output() -> None:
    landscape = review_store_relatives("landscape")
    assert landscape == (("review", "events.jsonl"), ("review", "store"))
    vertical = review_store_relatives("vertical")
    assert vertical == (("review", "events-vertical.jsonl"), ("review", "store-vertical"))
    assert vertical[0] != landscape[0]
    assert vertical[1] != landscape[1]


def test_chain_store_paths_keep_legacy_landscape() -> None:
    assert chain_store_relatives("landscape") == (
        ("run", "review-store", "events.jsonl"),
        ("run", "review-store"),
    )
    assert chain_store_relatives("vertical") == (
        ("run", "review-store-vertical", "events.jsonl"),
        ("run", "review-store-vertical"),
    )


def test_preview_and_approval_names_scope_per_output() -> None:
    assert preview_relatives("landscape") == ("previews", "preview.mp4")
    assert preview_relatives("vertical") == ("previews", "preview-vertical.mp4")
    assert preview_name_for("landscape") == "preview.mp4"
    assert preview_name_for("vertical") == "preview-vertical.mp4"
    assert approvals_relatives("landscape") == ("approvals", "records.jsonl")
    assert approvals_relatives("vertical") == ("approvals", "records-vertical.jsonl")
    assert preview_size_for("landscape") == (640, 360)
    assert preview_size_for("vertical") == (360, 640)


def test_subtitle_and_px_scaling() -> None:
    assert subtitle_chars_for_output(13, "landscape") == 13
    assert subtitle_chars_for_output(13, "vertical") == (13 * 1080) // 1920
    assert scale_px_for_output(96, "landscape") == 96
    assert scale_px_for_output(96, "vertical") == (96 * 1080) // 1920


def test_render_geometry_flows() -> None:
    landscape = geometry_for_output("landscape")
    assert (landscape.width, landscape.height) == (1920, 1080)
    vertical = geometry_for_output("vertical")
    assert (vertical.width, vertical.height) == (1080, 1920)
    with pytest.raises(ValidationError):
        RenderGeometry(output_id="vertical", width=1920, height=1080)


def test_frozen_preset_still_validates_landscape() -> None:
    preset = RenderPreset(
        video_format="MP4", video_codec="H264", width=1920, height=1080,
        audio_codec="aac", audio_sample_rate=48000, audio_channels=2,
    )
    section = RenderQcSection(
        schema_version="render-qc-v1",
        completion={
            "completion_field": "CompletionPercentage",
            "completion_value": 100,
            "status_strings_parsed": False,
            "select_all_frames": True,
            "marks_bound_render_extent": False,
        },
        preset=preset,
        qc_policy_id="qc-phase2-v1",
        external_credentials="none",
    )
    verify_render_geometry(section, None)
    verify_render_geometry(section, geometry_for_output("landscape"))
    with pytest.raises(RenderQcSmokeError, match="does not match"):
        verify_render_geometry(section, geometry_for_output("vertical"))


class _FakeProject:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}

    def SetSetting(self, key: str, value: str) -> bool:  # noqa: N802 (vendor API name)
        self.settings[key] = value
        return True


def test_apply_project_settings_geometry() -> None:
    manifest = Phase0AFixtureManifest.model_validate_json(
        Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json").read_bytes()
    )
    landscape = _FakeProject()
    apply_project_settings(landscape, manifest)  # type: ignore[arg-type]
    assert landscape.settings["timelineResolutionWidth"] == str(manifest.recipe.source.width)
    assert landscape.settings["timelineResolutionHeight"] == str(manifest.recipe.source.height)
    vertical = _FakeProject()
    apply_project_settings(vertical, manifest, geometry=VERTICAL_GEOMETRY)  # type: ignore[arg-type]
    assert vertical.settings["timelineResolutionWidth"] == "1080"
    assert vertical.settings["timelineResolutionHeight"] == "1920"


class _StubIr:
    episode_id = "ep-geo"


def test_render_native_steps_carry_geometry() -> None:
    caps = CapabilityView({}, {})
    landscape = render_native_steps(_StubIr(), caps, 100)  # type: ignore[arg-type]
    assert (landscape[0].normalized_params.width, landscape[0].normalized_params.height) == (
        1920, 1080,
    )
    vertical = render_native_steps(_StubIr(), caps, 100, output_id="vertical")  # type: ignore[arg-type]
    assert (vertical[0].normalized_params.width, vertical[0].normalized_params.height) == (
        1080, 1920,
    )


def test_telop_chapter_qa_canvases() -> None:
    assert canvas_size_for("landscape") == (1920, 1080)
    assert canvas_size_for("vertical") == (1080, 1920)
    assert chapter_canvas_for("vertical") == (1080, 1920)
    assert qa_canvas_for("vertical") == (1080, 1920)
    assert frame_bytes_for("vertical") == 1080 * 1920 * 3
    assert frame_bytes_for("landscape") == 1920 * 1080 * 3


def test_overlay_geometry_scales_for_vertical() -> None:
    landscape = geo_for("landscape")
    assert (landscape.timeline_width, landscape.timeline_height) == (1920, 1080)
    vertical = geo_for("vertical")
    assert (vertical.timeline_width, vertical.timeline_height) == (1080, 1920)
    assert vertical.safe_margin_px == scale_px_for_output(96, "vertical")


def test_subtitle_policy_rederives_wrap_per_output() -> None:
    policy = SubtitleQcPolicy(
        policy_id="sub-test",
        min_duration_frames=12,
        max_lines=2,
        max_chars_per_line=20,
        declared_style_refs=("default",),
        default_style_ref="default",
    )
    assert policy.for_output("landscape").max_chars_per_line == 20
    assert policy.for_output("vertical").max_chars_per_line == (20 * 1080) // 1920


def _landscape_manifest() -> SourceManifest:
    video = VideoStreamRecord(
        index=0,
        codec_type="video",
        codec_name="h264",
        time_base_num=1,
        time_base_den=30000,
        start_pts=0,
        duration_num=6,
        duration_den=1,
        r_frame_rate_num=30,
        r_frame_rate_den=1,
        avg_frame_rate_num=30,
        avg_frame_rate_den=1,
        width=1920,
        height=1080,
        pix_fmt="yuv420p",
        nb_frames=180,
        rotation_degrees=0,
        hdr=ingest_models.HdrSignaling(
            dolby_vision_rpu=False,
            dolby_vision_profile=None,
            hdr10_mastering_display=False,
            smpte2094=False,
            color_transfer=None,
        ),
    )
    audio = AudioStreamRecord(
        index=1,
        codec_type="audio",
        codec_name="pcm_s16le",
        time_base_num=1,
        time_base_den=48000,
        start_pts=0,
        duration_num=6,
        duration_den=1,
        sample_rate=48000,
        channels=2,
        channel_layout="stereo",
        start_offset_samples=0,
    )
    return SourceManifest(
        schema_version="source-manifest-v1",
        artifact_type="source-manifest",
        artifact_id="source-manifest-geo-test",
        producer=Producer(name="services.ingest", version="1"),
        content_hash="0" * 64,
        file=FileIdentity(path="/synthetic/geo.mov", size_bytes=1, sha256="a" * 64),
        container=ContainerInfo(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            format_long_name="QuickTime / MOV",
            nb_streams=2,
            duration_num=6,
            duration_den=1,
        ),
        streams=(video, audio),
        monotonicity=(
            StreamMonotonicity(
                stream_index=0,
                monotonic=True,
                first_violation_index=None,
                sampled_packets=200,
            ),
            StreamMonotonicity(
                stream_index=1,
                monotonic=True,
                first_violation_index=None,
                sampled_packets=200,
            ),
        ),
        vfr_evidence=None,
        edit_source_recipe=RecipePointer(
            recipe_id="p0b-cfr30",
            recipe_source="config/toolchains/pins/normalize-recipes.json",
            args_sha256="b" * 64,
        ),
        eligibility=Eligibility(verdict="supported", reasons=()),
        probe=ingest_models.ProbeRecord(
            ffprobe_path="/synthetic/ffprobe",
            ffprobe_sha256="c" * 64,
            arguments=("-v", "error"),
        ),
    )


def _class_request(
    expectation: ExpectedOrientation, width: int, height: int
) -> OrientationCheckRequest:
    return OrientationCheckRequest(
        render=Path("render.mp4"),
        report=FfprobeReport(
            streams=(FfprobeStream(codec_type="video", width=width, height=height),),
            format=FfprobeFormat(),
        ),
        render_streams_raw=(),
        expectation=expectation,
        ir=None,
        edit_source=None,
        frame_source=lambda _media, _at: [],
        factory=IssueFactory.for_policy(clean_policy(preset()), ("0" * 64,)),
    )


def test_orientation_declared_portrait_passes_vertical_render() -> None:
    manifest = _landscape_manifest()
    derived = expected_orientation(manifest)
    assert derived.display_portrait is False
    declared = expected_orientation(manifest, declared_orientation="portrait")
    assert declared.display_portrait is True
    assert check_orientation(_class_request(derived, 1080, 1920)) != ()
    assert check_orientation(_class_request(declared, 1080, 1920)) == ()


def test_orientation_landscape_behavior_unchanged() -> None:
    manifest = _landscape_manifest()
    derived = expected_orientation(manifest)
    assert check_orientation(_class_request(derived, 1920, 1080)) == ()
    assert check_orientation(_class_request(derived, 1080, 1920)) != ()
    with pytest.raises(ValueError, match="unknown declared orientation"):
        expected_orientation(manifest, declared_orientation="square")


def test_evidence_models_bind_output_id() -> None:
    facts = ExecutionFactsV1(episode_id="ep-1", domains=())
    assert facts.output_id == "landscape"
    assert ExecutionFactsV1(
        episode_id="ep-1", output_id="vertical", domains=()
    ).output_id == "vertical"


def test_rebuild_entry_defaults_to_landscape() -> None:
    entry = RebuildRequestEntry(sequence=1, stage_hint="plan")
    assert entry.output_id is None
    vertical = RebuildRequestEntry(sequence=2, stage_hint="plan", output_id="vertical")
    assert vertical.output_id == "vertical"
