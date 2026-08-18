"""Todo 43 acceptance (plan side): deterministic Edit Plan generation + reconciliation.

Edit Plans separate video and audio items into deterministic link groups, carry
field locks and provenance, and record the full decision ledger (keeps/adjusts
place items; removes — including planner budget drops — are recorded but place
nothing). Every identity is recomputed deterministically (LLM UUID continuity
is never trusted), and generation over the five frozen Phase-1 fixtures must
reproduce the frozen golden ``plan_items``/record tables exactly.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.editorial.candidate_models import CandidateSpan, ProposalProducer
from services.editorial.proposal_builder import build_selection_proposal
from services.foundation_io import canonical_model_bytes
from services.plan.constraint_planner import solve
from services.plan.edit_plan_generate import EditPlanGenerationError, generate
from services.plan.edit_plan_ids import compute_decision_id, compute_edit_item_id
from services.plan.edit_plan_models import (
    AudioSpanBinding,
    EditPlan,
    EditPlanItem,
    RecordSpan,
    SelectionPlanRef,
)
from services.plan.edit_plan_reconcile import EditPlanReconcileError, reconcile
from services.plan.planner_models import (
    AllocatedSpan,
    HandlePassThrough,
    OrderingTrace,
    PlannerSolution,
    SoftScoreBreakdown,
)
from services.validate.edit_commit_schema import tuplize
from tests.editorial.support import GOLDEN_EXPECTED_PATH, load_manifest
from tests.editorial.test_selection_proposal import index_for, pool_for
from tests.plan.support import (
    ALL_FIXTURE_IDS,
    candidate_id_of_segment,
    planner_input_for,
    pre_budget_document,
    segment_map,
)

if TYPE_CHECKING:
    from services.editorial.candidate_models import SelectionPlanProposal
    from services.plan.planner_models import PlannerInput

REF01 = "p1-ref-01-clean-ja"
REF02 = "p1-ref-02-pauses-fillers"
REF03 = "p1-ref-03-multi-take-must-include"
REF04 = "p1-ref-04-linked-av-offset"
REF05 = "p1-ref-05-review-mix"
EDIT_BASE = "edit-base-v0"
PRODUCER = ProposalProducer(
    model_role_id="constraint-planner", contract_version="todo43-v1"
)


def _selection_for(fixture_id: str) -> SelectionPlanProposal:
    manifest = load_manifest(fixture_id)
    return build_selection_proposal(
        episode_id=manifest.fixture_id,
        rules=manifest.editorial_rules,
        pool=pool_for(manifest),
        director_document=pre_budget_document(fixture_id),
        evidence_index=index_for(manifest),
        plan_base_version="plan-base-v0",
        model_role_id="editorial-director",
        contract_version="phase-1-editorial-v1",
        fixture_only=True,
    )


def selection_ref_for(fixture_id: str, selection: SelectionPlanProposal) -> SelectionPlanRef:
    return SelectionPlanRef(
        episode_id=fixture_id,
        plan_version="v1",
        plan_sha256=hashlib.sha256(canonical_model_bytes(selection)).hexdigest(),
        plan_artifact_id=f"selection-plan.{fixture_id}.v1",
    )


def _audio_bindings(fixture_id: str) -> tuple[AudioSpanBinding, ...]:
    manifest = load_manifest(fixture_id)
    mapping = segment_map(fixture_id)
    link_by_segment = {link.segment_id: link for link in manifest.av_links}
    return tuple(
        AudioSpanBinding(
            candidate_id=candidate.candidate_id,
            span=_binding_span(
                link_by_segment[mapping[candidate.candidate_id]].audio_span.start_frame,
                link_by_segment[mapping[candidate.candidate_id]].audio_span.end_frame,
            ),
        )
        for candidate in _selection_for(fixture_id).candidates
        if candidate.intent == "keep"
        and mapping[candidate.candidate_id] in link_by_segment
    )


def _binding_span(start_frame: int, end_frame: int) -> CandidateSpan:
    return CandidateSpan(
        start_frame=start_frame,
        end_frame=end_frame,
        rate_num=30,
        rate_den=1,
        start_ms=start_frame * 1000 // 30,
        end_ms=end_frame * 1000 // 30,
    )


def _solved(planner_input: PlannerInput) -> PlannerSolution:
    outcome = solve(planner_input)
    assert isinstance(outcome, PlannerSolution)
    return outcome


def generated_for(
    fixture_id: str,
    *,
    plan_base_version: str = EDIT_BASE,
    selection: SelectionPlanProposal | None = None,
    solution: PlannerSolution | None = None,
) -> EditPlan:
    selection_plan = selection if selection is not None else _selection_for(fixture_id)
    solved = solution if solution is not None else _solved(planner_input_for(fixture_id))
    return generate(
        selection_plan,
        solved,
        plan_ref=selection_ref_for(fixture_id, selection_plan),
        plan_base_version=plan_base_version,
        producer=PRODUCER,
        fixture_only=True,
        audio_bindings=_audio_bindings(fixture_id),
    )


def _golden_fixture(fixture_id: str) -> dict[str, object]:
    document: object = json.loads(Path(GOLDEN_EXPECTED_PATH).read_bytes())
    assert isinstance(document, dict)
    fixtures = document["fixtures"]
    assert isinstance(fixtures, dict)
    entry = fixtures[fixture_id]
    assert isinstance(entry, dict)
    return entry


def _golden_av_items(fixture_id: str) -> list[dict[str, object]]:
    items = _golden_fixture(fixture_id)["plan_items"]
    assert isinstance(items, list)
    return [row for row in items if isinstance(row, dict) and row["kind"] != "subtitle"]


def _golden_record_rows(fixture_id: str) -> list[dict[str, object]]:
    rows = _golden_fixture(fixture_id)["ir_records"]
    assert isinstance(rows, list)
    return [row for row in rows if isinstance(row, dict) and row["track_index"] in (1, 2)]


# ------------------------------------------------------------- determinism


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_generation_is_deterministic_and_ids_recompute(fixture_id: str) -> None:
    plan = generated_for(fixture_id)
    twin = generated_for(fixture_id)

    assert plan == twin
    assert canonical_model_bytes(plan) == canonical_model_bytes(twin)
    for item in plan.items:
        expected = compute_edit_item_id(
            source_id=item.source_ref.source_id,
            edit_source_sha=item.source_ref.edit_source_sha,
            span=item.span,
            track_kind=item.track_kind,
            decision_id=item.decision_id,
        )
        assert item.item_id == expected, "item ids recompute from normalized fields"
    for row in plan.decisions:
        expected = compute_decision_id(
            base_episode_id=plan.base_selection_plan.episode_id,
            base_plan_version=plan.base_selection_plan.plan_version,
            base_plan_sha256=plan.base_selection_plan.plan_sha256,
            candidate_id=row.candidate_id,
            kind=row.kind,
            parent_candidate_id=row.parent_candidate_id,
        )
        assert row.decision_id == expected, "decision ids recompute from the base"


def test_ids_are_stable_across_edit_chain_versions() -> None:
    first = generated_for(REF02, plan_base_version=EDIT_BASE)
    second = generated_for(REF02, plan_base_version="v1")

    assert {item.item_id for item in first.items} == {item.item_id for item in second.items}
    assert {row.decision_id for row in first.decisions} == {
        row.decision_id for row in second.decisions
    }
    assert first.proposal_id != second.proposal_id


# ------------------------------------------------------------- goldens


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_generated_items_and_record_spans_match_goldens(fixture_id: str) -> None:
    plan = generated_for(fixture_id)
    mapping = segment_map(fixture_id)

    ours: dict[tuple[str, str], EditPlanItem] = {}
    for item in plan.items:
        key = (mapping[item.provenance.candidate_id], item.track_kind)
        assert key not in ours, "every planner-selected candidate appears exactly once"
        ours[key] = item

    golden_by_key = {
        (str(row["segment_id"]), str(row["kind"])): row for row in _golden_av_items(fixture_id)
    }
    assert set(ours) == set(golden_by_key)
    for key, row in golden_by_key.items():
        item = ours[key]
        source_span = row["source_span"]
        record_span = row["span"]
        assert isinstance(source_span, dict)
        assert isinstance(record_span, dict)
        assert (item.span.start_frame, item.span.end_frame) == (
            source_span["start_frame"],
            source_span["end_frame"],
        )
        assert (item.record_span.start_frame, item.record_span.end_frame) == (
            record_span["start_frame"],
            record_span["end_frame"],
        )

    golden_rows = _golden_record_rows(fixture_id)
    video_rows = [
        (row["record_start"], row["record_end"], row["source_start"], row["source_end"])
        for row in golden_rows
        if row["track_index"] == 1
    ]
    audio_rows = [
        (row["record_start"], row["record_end"], row["source_start"], row["source_end"])
        for row in golden_rows
        if row["track_index"] == 2
    ]
    ours_video = [
        (i.record_span.start_frame, i.record_span.end_frame, i.span.start_frame, i.span.end_frame)
        for i in plan.items
        if i.track_kind == "video"
    ]
    ours_audio = [
        (i.record_span.start_frame, i.record_span.end_frame, i.span.start_frame, i.span.end_frame)
        for i in plan.items
        if i.track_kind == "audio"
    ]
    assert ours_video == [tuple(row) for row in video_rows]
    assert ours_audio == [tuple(row) for row in audio_rows]

    selection = _golden_fixture(fixture_id)["selection"]
    assert isinstance(selection, dict)
    assert plan.total_duration_frames == selection["total_selected_frames"]


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_generation_reconciles_against_selection_and_solution(fixture_id: str) -> None:
    plan = generated_for(fixture_id)
    selection = _selection_for(fixture_id)
    solution = _solved(planner_input_for(fixture_id))

    reconcile(plan, selection, solution)


def test_remove_decisions_are_recorded_per_golden_drops() -> None:
    for fixture_id in ALL_FIXTURE_IDS:
        plan = generated_for(fixture_id)
        mapping = segment_map(fixture_id)
        selection = _golden_fixture(fixture_id)["selection"]
        assert isinstance(selection, dict)

        removed = sorted(
            mapping[row.candidate_id] for row in plan.decisions if row.kind == "remove"
        )
        assert removed == sorted(str(seg) for seg in selection["dropped_ids"])

        budget = [row for row in plan.decisions if row.rule == "budget-dropped"]
        assert sorted(mapping[row.candidate_id] for row in budget) == sorted(
            str(seg) for seg in selection["budget_dropped"]
        )
        placed = {item.provenance.candidate_id for item in plan.items}
        for row in plan.decisions:
            if row.kind == "remove":
                assert row.candidate_id not in placed, "removes place no items"


def test_budget_dropped_candidate_carries_budget_rule() -> None:
    plan = generated_for(REF03)

    m7 = candidate_id_of_segment(REF03, "m7")
    row = next(entry for entry in plan.decisions if entry.candidate_id == m7)
    assert row.kind == "remove"
    assert row.rule == "budget-dropped"


def test_dormant_adjustment_is_recorded_not_placed() -> None:
    plan = generated_for(REF05)
    mapping = segment_map(REF05)

    adjusts = [row for row in plan.decisions if row.kind == "adjust"]
    assert [mapping[row.candidate_id] for row in adjusts] == ["s3"]
    placed = {item.provenance.candidate_id for item in plan.items}
    assert all(row.candidate_id not in placed for row in adjusts)
    for row in adjusts:
        assert row.parent_candidate_id is not None, "adjust decisions chain to their parent"


def test_selected_adjust_candidate_places_adjusted_span_with_parent_provenance() -> None:
    fixture_id = REF05
    selection = _selection_for(fixture_id)
    planner_input = planner_input_for(fixture_id)
    observations = {row.candidate_id: row for row in planner_input.observations}
    by_id = {row.candidate_id: row for row in selection.candidates}
    s1 = candidate_id_of_segment(fixture_id, "s1")
    s3_adjust = candidate_id_of_segment(fixture_id, "s3", intent="adjust")
    s3_keep = candidate_id_of_segment(fixture_id, "s3")
    s4 = candidate_id_of_segment(fixture_id, "s4")
    selected = (s1, s3_adjust, s4)
    lengths = (150, 120, 150)
    allocated = []
    scores = []
    handles = []
    cursor = 0
    for candidate_id, length in zip(selected, lengths, strict=True):
        candidate = by_id[candidate_id]
        allocated.append(
            AllocatedSpan(
                candidate_id=candidate_id,
                source_span=candidate.span,
                record_start_frame=cursor,
                record_end_frame=cursor + length,
            )
        )
        scores.append(
            SoftScoreBreakdown(
                candidate_id=candidate_id,
                kind=observations[candidate_id].kind,
                content_score=observations[candidate_id].content_score,
                clarity_score=observations[candidate_id].clarity_score,
                content_weight=2,
                clarity_weight=1,
                weighted_score=observations[candidate_id].weighted_score(
                    planner_input.weights
                ),
            )
        )
        handles.append(
            HandlePassThrough(
                candidate_id=candidate_id,
                head_frames=candidate.handles.head_frames,
                tail_frames=candidate.handles.tail_frames,
            )
        )
        cursor += length
    solution = PlannerSolution(
        episode_id=fixture_id,
        ordering_rule_id=planner_input.ordering.rule_id,
        selected_candidate_ids=selected,
        allocated=tuple(allocated),
        total_duration_frames=cursor,
        scores=tuple(scores),
        ordering_trace=OrderingTrace(
            rule_id=planner_input.ordering.rule_id,
            rule=planner_input.ordering.rule,
            handles=tuple(handles),
        ),
    )

    plan = generated_for(fixture_id, selection=selection, solution=solution)

    video = next(
        item
        for item in plan.items
        if item.track_kind == "video" and item.provenance.candidate_id == s3_adjust
    )
    assert (video.span.start_frame, video.span.end_frame) == (300, 420)
    assert video.provenance.parent_candidate_id == s3_keep
    decision = next(row for row in plan.decisions if row.candidate_id == s3_adjust)
    assert decision.kind == "adjust"
    assert decision.parent_candidate_id == s3_keep
    audio = next(
        item
        for item in plan.items
        if item.track_kind == "audio" and item.link_group_id == video.item_id
    )
    assert audio.record_span == video.record_span
    reconcile(plan, selection, solution)


# --------------------------------------------------------- links and locks


@pytest.mark.parametrize("fixture_id", ALL_FIXTURE_IDS)
def test_av_link_integrity_both_directions(fixture_id: str) -> None:
    plan = generated_for(fixture_id)
    videos = {item.item_id for item in plan.items if item.track_kind == "video"}

    for item in plan.items:
        if item.track_kind == "video":
            assert item.link_group_id == item.item_id, "video items anchor their link group"
        else:
            assert item.link_group_id in videos, "audio links resolve to a video item"
            anchor = next(v for v in plan.items if v.item_id == item.link_group_id)
            assert anchor.decision_id == item.decision_id
            assert anchor.record_span == item.record_span
            assert anchor.span.length == item.span.length
    assert len([i for i in plan.items if i.track_kind == "audio"]) == len(videos)


def test_broken_link_fails_at_reconcile_and_schema() -> None:
    plan = generated_for(REF02)
    audio = next(item for item in plan.items if item.track_kind == "audio")
    broken = audio.model_copy(update={"link_group_id": "0" * 64})
    tampered = plan.model_copy(
        update={"items": tuple(broken if i is audio else i for i in plan.items)}
    )

    with pytest.raises(EditPlanReconcileError) as error:
        reconcile(tampered, _selection_for(REF02))
    assert error.value.code == "broken_link"

    document = json.loads(canonical_model_bytes(plan))
    items = document["items"]
    assert isinstance(items, list)
    first_audio = next(
        row for row in items if isinstance(row, dict) and row["track_kind"] == "audio"
    )
    assert isinstance(first_audio, dict)
    first_audio["link_group_id"] = "0" * 64
    with pytest.raises(ValidationError, match="link"):
        EditPlan.model_validate(tuplize(document))


def test_items_carry_locks_and_provenance() -> None:
    plan = generated_for(REF01)

    for item in plan.items:
        assert item.locks == ()
        assert item.provenance.analyzer
        assert item.provenance.rule == "p1-ordering-v1"
        assert item.provenance.candidate_id

    locked = plan.items[0].model_copy(update={"locks": ("source_span",)})
    assert locked.locks == ("source_span",)
    duplicated = json.loads(canonical_model_bytes(plan))
    assert isinstance(duplicated, dict)
    items = duplicated["items"]
    assert isinstance(items, list)
    first = items[0]
    assert isinstance(first, dict)
    first["locks"] = ["order", "order"]
    with pytest.raises(ValidationError, match="duplicate_lock"):
        EditPlan.model_validate(tuplize(duplicated))


# --------------------------------------------------------- malformed input


def test_llm_uuid_fields_are_rejected_at_model_level() -> None:
    plan = generated_for(REF02)
    document = json.loads(canonical_model_bytes(plan))
    assert isinstance(document, dict)
    document["decision_uuid"] = "llm-authored"
    with pytest.raises(ValidationError, match="llm_uuid_forbidden"):
        EditPlan.model_validate(document)

    document2 = json.loads(canonical_model_bytes(plan))
    assert isinstance(document2, dict)
    items = document2["items"]
    assert isinstance(items, list)
    first = items[0]
    assert isinstance(first, dict)
    first["llm_uuid"] = "also-forbidden"
    with pytest.raises(ValidationError, match="llm_uuid_forbidden"):
        EditPlan.model_validate(document2)


def test_resolve_fields_are_rejected_at_model_level() -> None:
    plan = generated_for(REF02)
    document = json.loads(canonical_model_bytes(plan))
    assert isinstance(document, dict)
    document["resolve_track_index"] = 1
    with pytest.raises(ValidationError, match="resolve_field_forbidden"):
        EditPlan.model_validate(document)


def test_av_length_mismatch_is_refused_at_generation() -> None:
    with pytest.raises(EditPlanGenerationError) as error:
        generate(
            _selection_for(REF01),
            _solved(planner_input_for(REF01)),
            plan_ref=selection_ref_for(REF01, _selection_for(REF01)),
            plan_base_version=EDIT_BASE,
            producer=PRODUCER,
            fixture_only=True,
            audio_bindings=(
                AudioSpanBinding(
                    candidate_id=candidate_id_of_segment(REF01, "s1"),
                    span=_binding_span(8, 160),
                ),
            ),
        )
    assert error.value.code == "av_length_mismatch"


def test_record_span_model_rejects_empty_ranges() -> None:
    with pytest.raises(ValidationError, match="record_span_empty"):
        RecordSpan(start_frame=10, end_frame=10)
