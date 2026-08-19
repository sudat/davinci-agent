"""Todo-47 acceptance: package compiles match the frozen independent goldens.

Each frozen Phase-2 fixture scenario compiles through the real adapter and
the resulting instruction tables must equal the independently derived golden
tables — never the compiler's own output. Determinism is proven by
double-compilation byte identity and the content-hash chain.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from services.foundation_io import canonical_model_bytes
from services.resolve_adapter.package import PackageCompileRequest, compile_resolve_package
from services.toolchain.render_qc import render_complete
from tests.resolve_adapter.support import (
    declared_media,
    golden_fixture,
    ir_for,
    load_p2_manifest,
    lock_sha256,
    phase2_lock,
)

if TYPE_CHECKING:
    from services.fixtures.manifest_phase2 import Phase2FixtureManifest
    from services.resolve_adapter.models import ResolvePackage

ALL_FIXTURES = (
    "p2-stale-capability",
    "p2-partial-build-restart",
    "p2-same-duration-wrong-media",
    "p2-false-render-complete",
    "p2-blocking-qc-privacy",
)


def compile_fixture(fixture_id: str, **overrides: object) -> ResolvePackage:
    manifest = load_p2_manifest(fixture_id)
    base: dict[str, object] = {
        "ir": ir_for(manifest),
        "lock": phase2_lock(),
        "lock_sha256": lock_sha256(),
        "declared_media": declared_media(manifest),
        "artifact_id": f"resolve-package-{fixture_id}",
    }
    base.update(overrides)
    return compile_resolve_package(PackageCompileRequest(**base))  # type: ignore[arg-type]


def test_each_fixture_package_matches_the_frozen_golden_tables() -> None:
    for fixture_id in ALL_FIXTURES:
        package = compile_fixture(fixture_id)
        golden = golden_fixture(fixture_id)["package"]
        assert isinstance(golden, dict)
        view = package.model_dump(mode="json")
        for section in ("timeline", "track_map", "placements", "link_groups", "render_job"):
            assert view[section] == golden[section], f"{fixture_id}:{section}"
        if golden["subtitle_step"] is None:
            assert package.subtitle_step is None
        else:
            assert view["subtitle_step"] == golden["subtitle_step"]
        inputs_view = golden["inputs_view"]
        assert isinstance(inputs_view, dict)
        matrix = inputs_view["capability_matrix"]
        assert isinstance(matrix, dict)
        assert view["inputs_view"]["capability_matrix_path"] == matrix["path"]
        assert view["inputs_view"]["capability_matrix_sha256"] == matrix["sha256"]
        assert view["inputs_view"]["declared_media"] == [inputs_view["declared_media"]]
        assert view["artifact_type"] == "resolve_package_v1"


def test_placements_carry_only_validated_operations_and_absolute_frames() -> None:
    package = compile_fixture("p2-partial-build-restart")
    for placement in package.placements:
        assert placement.api_operation == "AppendToTimeline"
        assert placement.capability in ("base_cut", "media_intro_outro")
        assert placement.clip_info.record_frame >= 108000
        assert placement.clip_info.track_index == 1
    linked = {item for group in package.link_groups for item in group.item_ids}
    assert len(linked) == len(package.placements)


def test_subtitle_step_is_external_post_render_with_anchor_frames() -> None:
    package = compile_fixture("p2-blocking-qc-privacy")
    assert package.subtitle_step is not None
    step = package.subtitle_step
    assert step.rung == "external"
    assert step.mux_operation == "ffmpeg-mov-text"
    assert any(arg == "mov_text" for arg in step.argv)
    for cue in step.cues:
        assert cue.anchor_record_start_frame < cue.anchor_record_end_frame
        assert cue.end_ms > cue.start_ms
    empty = compile_fixture("p2-stale-capability")
    assert empty.subtitle_step is None


def test_render_job_freezes_completion_contract_and_extent() -> None:
    package = compile_fixture("p2-false-render-complete")
    job = package.render_job
    assert job.completion.field == "CompletionPercentage"
    assert job.completion.value == 100
    assert job.completion.status_strings_parsed is False
    assert job.completion.marks_bound_render_extent is False
    assert job.select_all_frames is True
    assert (job.video_format, job.video_codec, job.width, job.height) == ("MP4", "H264", 1920, 1080)
    assert (job.audio_codec, job.audio_sample_rate, job.audio_channels) == ("aac", 48000, 2)
    assert job.frame_origin == 108000
    assert job.timeline_start_timecode == "01:00:00:00"
    assert job.extent_frames == 450


def test_false_render_complete_fault_stays_incomplete_under_the_frozen_rule() -> None:
    manifest: Phase2FixtureManifest = load_p2_manifest("p2-false-render-complete")
    assert manifest.fault.kind == "false-render-complete"
    assert render_complete(
        {"JobStatus": manifest.fault.reported_job_status}
        | {
            "CompletionPercentage": manifest.fault.reported_completion_percentage
        }
    ) is False
    assert render_complete({"CompletionPercentage": 100, "JobStatus": "未完了"}) is True


def test_double_compilation_is_byte_identical_with_hash_chain() -> None:
    for fixture_id in ALL_FIXTURES:
        first = compile_fixture(fixture_id)
        second = compile_fixture(fixture_id)
        assert canonical_model_bytes(first) == canonical_model_bytes(second)
        zeroed = first.model_copy(update={"content_hash": "0" * 64})
        assert first.content_hash == hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()


def test_distinct_artifact_ids_produce_distinct_packages() -> None:
    manifest = load_p2_manifest("p2-stale-capability")
    one = compile_fixture("p2-stale-capability")
    two = compile_resolve_package(
        PackageCompileRequest(
            ir=ir_for(manifest),
            lock=phase2_lock(),
            lock_sha256=lock_sha256(),
            declared_media=declared_media(manifest),
            artifact_id="resolve-package-p2-stale-capability-alt",
        )
    )
    assert one.content_hash != two.content_hash


@pytest.mark.parametrize(
    ("fixture_id", "route_field", "expected"),
    [
        ("p2-stale-capability", "package_compilation", "typed-failure"),
        ("p2-stale-capability", "human_route", "refresh-capability-matrix"),
        ("p2-partial-build-restart", "retry", "clean-rebuild-restart"),
        ("p2-same-duration-wrong-media", "failure_code", "media-hash-drift"),
        ("p2-false-render-complete", "render", "refused-incomplete"),
        ("p2-blocking-qc-privacy", "qc", "typed-failure-blocks-publish"),
    ],
)
def test_manifest_routes_agree_with_frozen_golden_routes(
    fixture_id: str, route_field: str, expected: str
) -> None:
    manifest = load_p2_manifest(fixture_id)
    golden = golden_fixture(fixture_id)["expected_route"]
    assert isinstance(golden, dict)
    assert golden[route_field] == expected
    fault = manifest.fault.model_dump(mode="json")
    assert fault["expected_retry"] == golden["retry"]
    assert fault["expected_human_route"] == golden["human_route"]
