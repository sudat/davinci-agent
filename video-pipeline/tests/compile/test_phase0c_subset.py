from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.compile.classify import ClassifyError, decide
from services.compile.phase0c import CompileError, apply_command, build_ir, compile_plan
from services.conform.errors import CoordinateOverflowError
from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
    ItemIdSelector0C,
    ReviewCommand0C,
    SubtitleTextSelector0C,
)
from services.contracts.primitives import Producer, RationalFrameRate, SourceFrameSpan
from services.contracts.timeline_ir import TimelineIr0C
from services.fixtures.manifest_phase0c import PHASE_0C_FIXTURE_IDS, Phase0CFixtureManifest
from services.foundation_io import canonical_model_bytes

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-0c")
GOLDEN = json.loads(Path("tests/goldens/reference/phase-0c/expected.json").read_bytes())
GOLDEN_FIXTURES = GOLDEN["fixtures"]
RATE = RationalFrameRate(num=30, den=1)
TEST_PRODUCER = Producer(name="phase0c-test", version="1")


def load_manifest(fixture_id: str) -> Phase0CFixtureManifest:
    return Phase0CFixtureManifest.model_validate_json(
        (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
    )


def manifest_to_plan(manifest: Phase0CFixtureManifest) -> EditPlan0C:
    source = manifest.edit_plan.edit_source
    items = tuple(
        EditPlanItem0C(
            item_id=item.item_id,
            kind=item.kind,
            source_id=item.source_id,
            span=SourceFrameSpan(
                start_frame=item.span.start_frame,
                end_frame=item.span.end_frame,
                rate=RATE,
            ),
            track_index=item.track_index,
            av_link_id=item.av_link_id,
            subtitle_text=item.subtitle_text,
            locked_fields=item.locked_fields,
        )
        for item in manifest.edit_plan.items
    )
    body = EditPlanBody0C(
        plan_version="v1",
        edit_source=EditSourceRef0C(
            source_id=source.source_id,
            total_frames=source.total_frames,
        ),
        items=items,
    )
    return EditPlan0C(
        artifact_id=f"edit-plan-{manifest.fixture_id}",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=TEST_PRODUCER,
        inputs=(),
        frame_rate=RATE,
        plan=body,
    )


def manifest_to_command(manifest: Phase0CFixtureManifest) -> ReviewCommand0C:
    spec = manifest.command
    if spec.target.kind == "item_id":
        target = ItemIdSelector0C(kind="item_id", item_id=spec.target.item_id)
    else:
        target = SubtitleTextSelector0C(kind="subtitle_text_match", text=spec.target.text)
    new_span = None
    if spec.new_span is not None:
        new_span = SourceFrameSpan(
            start_frame=spec.new_span.start_frame,
            end_frame=spec.new_span.end_frame,
            rate=RATE,
        )
    return ReviewCommand0C(
        command_id=f"cmd-{manifest.fixture_id}",
        language=spec.language,
        instruction=spec.instruction,
        operation=spec.operation,
        base_plan_version="v1",
        target=target,
        new_span=new_span,
        new_text=spec.new_text,
    )


def compile_fixture(fixture_id: str):
    manifest = load_manifest(fixture_id)
    return compile_plan(
        manifest_to_plan(manifest),
        manifest_to_command(manifest),
        f"timeline-ir-{fixture_id}",
    )


def test_three_clear_cases_match_frozen_golden_tables_exactly() -> None:
    for fixture_id in ("p0c-remove-clear", "p0c-span-clear", "p0c-subtitle-clear"):
        result = compile_fixture(fixture_id)
        golden = GOLDEN_FIXTURES[fixture_id]

        assert result.decision.classification == golden["classification"] == "clear"
        assert result.decision.action == "apply"
        assert result.plan.plan.plan_version == golden["resulting_plan_version"] == "v2"
        assert [
            item.item_id for item in result.plan.plan.items
        ] == [entry["item_id"] for entry in golden["plan_items"]]
        for item, expected in zip(result.plan.plan.items, golden["plan_items"], strict=True):
            assert item.span.start_frame == expected["span"]["start_frame"]
            assert item.span.end_frame == expected["span"]["end_frame"]
            assert item.subtitle_text == expected["subtitle_text"]
            assert item.av_link_id == expected["av_link_id"]
        rows = [
            (
                ir_item.item_id,
                ir_item.record_span.start_frame,
                ir_item.record_span.end_frame,
                ir_item.source.span.start_frame,
                ir_item.source.span.end_frame,
                ir_item.subtitle_text,
            )
            for track in result.ir.tracks
            for ir_item in track.items
        ]
        expected_rows = [
            (
                entry["item_id"],
                entry["record_start"],
                entry["record_end"],
                entry["source_start"],
                entry["source_end"],
                entry["subtitle_text"],
            )
            for entry in golden["record_table"]
        ]
        assert rows == expected_rows


def test_remove_clear_drops_link_group_and_shifts_downstream_records() -> None:
    result = compile_fixture("p0c-remove-clear")

    remaining = {item.item_id for item in result.plan.plan.items}
    assert remaining == {"v1", "v3", "a1", "a3"}
    records = {
        ir_item.item_id: ir_item.record_span
        for track in result.ir.tracks
        for ir_item in track.items
    }
    assert (records["v1"].start_frame, records["v1"].end_frame) == (0, 150)
    assert (records["v3"].start_frame, records["v3"].end_frame) == (150, 300)
    assert (records["a3"].start_frame, records["a3"].end_frame) == (150, 300)


def test_ambiguous_command_defers_without_plan_mutation() -> None:
    manifest = load_manifest("p0c-ambiguous-two-targets")
    plan = manifest_to_plan(manifest)
    before = canonical_model_bytes(plan)

    result = compile_plan(plan, manifest_to_command(manifest), "timeline-ir-ambiguous")

    assert result.decision.classification == "ambiguous"
    assert result.decision.action == "defer"
    assert result.decision.conflict is None
    assert list(result.decision.target_candidate_item_ids) == ["s1", "s2"]
    assert result.plan is plan
    assert result.plan.plan.plan_version == "v1"
    assert canonical_model_bytes(result.plan) == before


def test_conflict_command_defers_and_records_the_locked_field() -> None:
    manifest = load_manifest("p0c-locked-conflict")
    plan = manifest_to_plan(manifest)
    before = canonical_model_bytes(plan)

    result = compile_plan(plan, manifest_to_command(manifest), "timeline-ir-conflict")

    assert result.decision.classification == "conflict"
    assert result.decision.action == "defer"
    assert result.decision.conflict is not None
    assert result.decision.conflict.target_item_id == "v2"
    assert result.decision.conflict.locked_field == "span"
    assert result.plan is plan
    assert canonical_model_bytes(result.plan) == before


def test_compilation_is_deterministic_with_stable_hashes() -> None:
    for fixture_id in PHASE_0C_FIXTURE_IDS:
        first = compile_fixture(fixture_id)
        second = compile_fixture(fixture_id)
        assert first.plan_sha256() == second.plan_sha256()
        assert first.ir_sha256() == second.ir_sha256()
        assert canonical_model_bytes(first.ir) == canonical_model_bytes(second.ir)
        assert first.ir.content_hash == second.ir.content_hash
        if fixture_id in ("p0c-remove-clear", "p0c-span-clear", "p0c-subtitle-clear"):
            assert first.ir.content_hash == first.plan.content_hash != "0" * 64


def test_apply_cannot_be_forced_through_a_deferred_decision() -> None:
    manifest = load_manifest("p0c-ambiguous-two-targets")
    plan = manifest_to_plan(manifest)
    command = manifest_to_command(manifest)

    with pytest.raises(CompileError, match="only clear commands"):
        apply_command(plan, command)


def test_unresolved_anchor_is_an_explicit_error() -> None:
    manifest = load_manifest("p0c-remove-clear")
    plan = manifest_to_plan(manifest)
    command = ReviewCommand0C(
        command_id="cmd-missing",
        language="ja",
        instruction="99番目のセグメントを削除してください",
        operation="remove_segment",
        base_plan_version="v1",
        target=ItemIdSelector0C(kind="item_id", item_id="v99"),
    )

    with pytest.raises(CompileError, match="unresolved anchor"):
        compile_plan(plan, command, "timeline-ir-missing")


def test_stale_base_plan_version_is_rejected_by_the_classifier() -> None:
    manifest = load_manifest("p0c-remove-clear")
    plan = manifest_to_plan(manifest)
    command = ReviewCommand0C(
        command_id="cmd-stale",
        language="ja",
        instruction="2番目のセグメントを削除してください",
        operation="remove_segment",
        base_plan_version="v2",
        target=ItemIdSelector0C(kind="item_id", item_id="v2"),
    )

    with pytest.raises(ClassifyError, match="targets plan v2"):
        decide(plan, command)


def test_resolve_specific_field_in_plan_is_rejected() -> None:
    manifest = load_manifest("p0c-remove-clear")
    payload = json.loads(canonical_model_bytes(manifest_to_plan(manifest)))
    payload["plan"]["items"][0]["resolve_track_index"] = 7

    with pytest.raises(ValidationError, match="resolve_field_forbidden"):
        EditPlan0C.model_validate_json(json.dumps(payload))


def test_resolve_specific_field_in_ir_is_rejected() -> None:
    result = compile_fixture("p0c-remove-clear")
    payload = json.loads(canonical_model_bytes(result.ir))
    payload["tracks"][0]["items"][0]["resolve_clip_color"] = "blue"

    with pytest.raises(ValidationError, match="resolve_field_forbidden"):
        TimelineIr0C.model_validate_json(json.dumps(payload))


def test_float_frame_value_is_rejected() -> None:
    manifest = load_manifest("p0c-span-clear")
    payload = json.loads(canonical_model_bytes(manifest_to_plan(manifest)))
    payload["plan"]["items"][0]["span"]["start_frame"] = 0.5

    with pytest.raises(ValidationError):
        EditPlan0C.model_validate(payload)


def test_record_overflow_is_guarded() -> None:
    manifest = load_manifest("p0c-remove-clear")
    plan = manifest_to_plan(manifest)
    huge = (1 << 63) - 1
    items = tuple(
        item.model_copy(
            update={
                "span": SourceFrameSpan(
                    start_frame=0,
                    end_frame=huge,
                    rate=RATE,
                )
            }
        )
        if item.item_id == "v1"
        else item
        for item in plan.plan.items
    )
    edited_body = plan.plan.model_copy(
        update={
            "items": items,
            "edit_source": plan.plan.edit_source.model_copy(update={"total_frames": huge}),
        }
    )
    edited = plan.model_copy(update={"plan": edited_body})

    with pytest.raises(CoordinateOverflowError, match="record end"):
        build_ir(edited, "timeline-ir-overflow", ())


def test_subtitle_ir_extension_carries_text_and_kind() -> None:
    result = compile_fixture("p0c-subtitle-clear")

    subtitle_tracks = [track for track in result.ir.tracks if track.track.kind == "subtitle"]
    assert len(subtitle_tracks) == 1
    cue = subtitle_tracks[0].items[0]
    assert cue.subtitle_text == "最初のセグメントでした"
    assert cue.record_span.start_frame == 0
    assert cue.record_span.end_frame == 150
