"""Trace coverage semantics: regeneration mapping and rejection of broken traces."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.compile.phase0c import compile_plan
from services.contracts.primitives import RecordFrameSpan
from services.contracts.timeline_ir import TimelineIr0C
from services.foundation_io import canonical_model_bytes
from services.preview.binding import initial_bindings, initial_timeline_ir
from services.preview.models import PreviewLayoutError, PreviewMediaBindings, PreviewTraceManifest
from services.preview.render import render_preview
from tests.preview.conftest import p0c_plan_and_command

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest
    from services.preview.tools import PinnedTools


def test_regenerated_preview_maps_shifted_ranges_to_the_applied_decision(
    p0c_context: tuple[PreviewTraceManifest, PreviewTraceManifest],
) -> None:
    trace_v1, trace_v2 = p0c_context
    assert trace_v1.timeline_binding.plan_version == "v1"
    assert trace_v1.timeline_binding.total_record_frames == 450
    assert trace_v2.timeline_binding.plan_version == "v2"
    assert trace_v2.timeline_binding.total_record_frames == 300
    coverage = [
        (entry.span.start_frame, entry.span.end_frame, entry.decision_id)
        for entry in trace_v2.record_to_decision
    ]
    assert coverage == [(0, 150, "initial-plan-v1"), (150, 300, "cmd-p0c-remove-clear")]
    decision_ids = {decision.decision_id for decision in trace_v2.decisions}
    assert {"initial-plan-v1", "cmd-p0c-remove-clear"} <= decision_ids
    removed = {decision.case_id for decision in trace_v2.decisions if decision.applied}
    assert "p0c-remove-clear" in removed
    summary = trace_v2.ffprobe_summary
    assert (summary.stream_count, summary.nb_read_frames, summary.video_duration_ms) == (
        2,
        300,
        10000,
    )
    assert summary.subtitle_codec is None
    assert trace_v2.strategy_notes.subtitle_rung == "omitted-no-subtitle-items"
    item_ids = {entry.item_id for entry in trace_v2.inputs}
    assert item_ids == {"v1", "v3", "a1", "a3"}
    records = {
        entry.item_id: (entry.record_span.start_frame, entry.record_span.end_frame)
        for entry in trace_v2.inputs
    }
    assert records["v3"] == (150, 300)
    assert records["a3"] == (150, 300)


def test_coverage_gap_is_rejected(
    initial_render: tuple[PreviewTraceManifest, Path],
) -> None:
    trace, _ = initial_render
    payload = json.loads(canonical_model_bytes(trace))
    payload["record_to_decision"] = payload["record_to_decision"][:2]
    with pytest.raises(ValidationError, match="coverage"):
        PreviewTraceManifest.model_validate(payload)


def test_unknown_decision_reference_is_rejected(
    initial_render: tuple[PreviewTraceManifest, Path],
) -> None:
    trace, _ = initial_render
    payload = json.loads(canonical_model_bytes(trace))
    payload["record_to_decision"][1]["decision_id"] = "ghost-decision"
    with pytest.raises(ValidationError, match="unknown decision"):
        PreviewTraceManifest.model_validate(payload)


def test_overlapping_coverage_is_rejected(
    initial_render: tuple[PreviewTraceManifest, Path],
) -> None:
    trace, _ = initial_render
    payload = json.loads(canonical_model_bytes(trace))
    payload["record_to_decision"][1]["span"]["start_frame"] = 29
    with pytest.raises(ValidationError, match="coverage"):
        PreviewTraceManifest.model_validate(payload)


def test_float_field_is_rejected(
    initial_render: tuple[PreviewTraceManifest, Path],
) -> None:
    trace, _ = initial_render
    payload = json.loads(canonical_model_bytes(trace))
    payload["ffprobe_summary"]["width"] = 640.0
    with pytest.raises(ValidationError):
        PreviewTraceManifest.model_validate(payload)


def test_malformed_ir_json_is_rejected(manifest: Phase0AFixtureManifest) -> None:
    payload = json.loads(canonical_model_bytes(initial_timeline_ir(manifest)))
    payload["tracks"][0]["items"][0]["record_span"]["start_frame"] = 0.5
    with pytest.raises(ValidationError):
        TimelineIr0C.model_validate(payload)


def test_ir_with_record_gap_is_refused_before_render(
    tools: PinnedTools,
    fixture_dir: Path,
    manifest: Phase0AFixtureManifest,
    tmp_path: Path,
) -> None:
    ir = initial_timeline_ir(manifest)
    video_track = next(track for track in ir.tracks if track.track.kind == "video")
    shifted = video_track.items[1].model_copy(
        update={"record_span": RecordFrameSpan(start_frame=40, end_frame=330)}
    )
    tampered_tracks = tuple(
        track.model_copy(update={"items": (track.items[0], shifted, *track.items[2:])})
        if track.track.kind == "video"
        else track
        for track in ir.tracks
    )
    tampered = ir.model_copy(update={"tracks": tampered_tracks})
    with pytest.raises(PreviewLayoutError, match="gap/overlap"):
        render_preview(None, tampered, initial_bindings(fixture_dir), tmp_path, tools=tools)
    assert not (tmp_path / "preview.mp4").exists()


def test_ir_disagreeing_with_the_edit_plan_is_refused(
    tools: PinnedTools,
    fixture_dir: Path,
    manifest: Phase0AFixtureManifest,
    tmp_path: Path,
) -> None:
    plan, _ = p0c_plan_and_command()
    ir = initial_timeline_ir(manifest)
    bindings = initial_bindings(fixture_dir)
    assert isinstance(bindings, PreviewMediaBindings)
    with pytest.raises(PreviewLayoutError, match="do not match the edit plan"):
        render_preview(plan, ir, bindings, tmp_path, tools=tools)


def test_trace_ir_sha_is_the_canonical_ir_digest(
    p0c_context: tuple[PreviewTraceManifest, PreviewTraceManifest],
) -> None:
    _, trace_v2 = p0c_context
    plan, command = p0c_plan_and_command()
    result = compile_plan(plan, command, "timeline-ir-preview-p0c-v2")
    expected = hashlib.sha256(canonical_model_bytes(result.ir)).hexdigest()
    assert trace_v2.timeline_binding.ir_sha256 == expected
