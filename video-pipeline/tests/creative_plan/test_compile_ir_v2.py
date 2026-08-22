"""Timeline IR v2 + CreativeEditPlanProposalV2 compiler tests (task 31).

Given a REAL Director v2 three-pass result over the shared synthetic W3
fixture (tests/editorial_v2/fixtures/three_pass_fixture.py), the lite
CreativeEditDraft is upgraded into a full CreativeEditPlanProposalV2 and
compiled into Timeline IR v2. Locked behaviors:

(a) plan + IR round-trip through canonical JSON;
(b) Decision ID linkage — every placed span carries a candidate_ref the
    selection covers (no orphan placements);
(c) track/role representation — B-roll on the b_roll video track, subtitle
    cues as subtitle cues (never video items), audio roles separated;
(d) unknown op kinds and unverified speed changes are rejected;
(e) determinism — double compile is byte-identical;
(f) source-to-record consistency — primary record spans contiguous with
    handle math, overlays inside their anchor spans.

Adversarial probes: unknown-candidate (malformed), determinism (stale
state), hand-built gapped primary rejected at the IR model itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from services.contracts.primitives import RationalFrameRate
from services.creative_plan.compile_ir_v2 import (
    CompileIrV2Error,
    SourceFactsV2,
    SourceFactV2,
    TimelineIrV2,
    TranscriptFactV2,
    compile_ir_v2,
)
from services.creative_plan.edit_models_v2 import (
    CreativeEditPlanProposalV2,
    PresentationIntentRecordV2,
    SelectionRefV2,
    TitleCardOp,
    upgrade_from_creative_draft,
)
from services.editorial_v2.director_v2 import DirectorV2
from services.editorial_v2.moment_models import (
    MomentCandidateV2,
    MomentHandles,
    MomentProvenance,
    MomentSelectionProposalV2,
    MomentSourceSpan,
)
from services.foundation_io import canonical_model_bytes
from tests.editorial_v2.fixtures.three_pass_fixture import (
    EPISODE_ID,
    SOURCE_ID,
    make_brief,
    make_episode_artifact,
    open_api,
)

RATE_30 = RationalFrameRate(num=30, den=1)


def _facts(duration: int = 1000, *transcripts: TranscriptFactV2) -> SourceFactsV2:
    return SourceFactsV2(
        rate=RATE_30,
        sources=(SourceFactV2(source_id=SOURCE_ID, duration_frames=duration),),
        transcripts=transcripts,
    )


def _candidate(
    cid: str, start: int, end: int, *, handles: MomentHandles | None = None, kind: str = "speech"
) -> MomentCandidateV2:
    return MomentCandidateV2(
        candidate_id=cid,
        candidate_type=kind,  # type: ignore[arg-type]
        source_span=MomentSourceSpan(start_frame=start, end_frame=end),
        intent="keep",
        rationale="synthetic test candidate",
        evidence_refs=(f"shot-{cid}",),
        confidence=0.8,
        handles=handles,
        provenance=MomentProvenance(producer="test", version="1"),
    )


def _plan(
    ops: object, proposal_id: str = "cep-t", episode_id: str = "ep-t"
) -> CreativeEditPlanProposalV2:
    return CreativeEditPlanProposalV2.model_validate(
        {
            "schema_version": "creative-edit-plan-v2",
            "proposal_id": proposal_id,
            "episode_id": episode_id,
            "selection_ref": {"proposal_id": f"msel-{episode_id}"},
            "operations": ops,
        }
    )


def _selection(
    *candidates: MomentCandidateV2, episode_id: str = "ep-t"
) -> MomentSelectionProposalV2:
    return MomentSelectionProposalV2(
        proposal_id=f"msel-{episode_id}",
        episode_id=episode_id,
        candidates=candidates,
    )


@pytest.fixture(scope="module")
def compiled(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """Real Director v2 output over the W3 fixture, upgraded and compiled."""
    tmp = tmp_path_factory.mktemp("task31-fixture")
    with open_api(make_episode_artifact(), tmp) as api:
        result = DirectorV2().run_three_pass(make_brief(), api)
    selection = result.moment_selection.proposal
    operations = upgrade_from_creative_draft(
        result.creative_edit,
        selection,
        b_roll_matches=result.moment_selection.b_roll_matches,
    )
    plan = CreativeEditPlanProposalV2(
        schema_version="creative-edit-plan-v2",
        proposal_id="cep-ep-w3",
        episode_id=selection.episode_id,
        selection_ref=SelectionRefV2(proposal_id=selection.proposal_id),
        operations=operations,
        presentation_intents=(
            PresentationIntentRecordV2(
                intent_id="pi-001",
                semantic_kind="emphasis_punch_in",
                note="task-32 seam slot",
            ),
        ),
    )
    facts = _facts(
        610,
        TranscriptFactV2(
            segment_id="tr-a1",
            source_id=SOURCE_ID,
            text="today we review the new camera gear",
            start_frame=10,
            end_frame=90,
        ),
        TranscriptFactV2(
            segment_id="tr-c1",
            source_id=SOURCE_ID,
            text="the camera weighs only 400 grams",
            start_frame=210,
            end_frame=290,
        ),
        TranscriptFactV2(
            segment_id="tr-e1",
            source_id=SOURCE_ID,
            text="so anyway i kept walking around the park",
            start_frame=510,
            end_frame=590,
        ),
    )
    ir = compile_ir_v2(selection, plan, source_facts=facts)
    return SimpleNamespace(result=result, selection=selection, plan=plan, facts=facts, ir=ir)


# ------------------------------------------------------------ (a) round-trip


def test_full_fifteen_kind_plan_round_trips() -> None:
    ops = [
        {"op_id": "op-01", "kind": "place_primary_clip", "candidate_ref": "cand-s1"},
        {"op_id": "op-02", "kind": "place_primary_clip", "candidate_ref": "cand-s2"},
        {
            "op_id": "op-03",
            "kind": "b_roll_overlay",
            "candidate_ref": "cand-b1",
            "anchor_candidate_ref": "cand-s2",
        },
        {
            "op_id": "op-04",
            "kind": "cutaway",
            "candidate_ref": "cand-r1",
            "anchor_candidate_ref": "cand-s1",
        },
        {
            "op_id": "op-05",
            "kind": "split_edit",
            "style": "j_cut",
            "a_candidate_ref": "cand-s1",
            "b_candidate_ref": "cand-s2",
            "lead_frames": 10,
        },
        {"op_id": "op-06", "kind": "subtitle_track", "target_candidate_ref": "cand-s1"},
        {
            "op_id": "op-07",
            "kind": "title_card",
            "variant": "lower_third",
            "target_candidate_ref": "cand-s1",
            "text": "400g travel camera",
            "duration_frames": 30,
        },
        {
            "op_id": "op-08",
            "kind": "punch_in",
            "target_candidate_ref": "cand-s2",
            "mode": "punch_in",
        },
        {
            "op_id": "op-09",
            "kind": "place_still",
            "candidate_ref": "cand-st1",
            "anchor_candidate_ref": "cand-s2",
            "duration_frames": 45,
        },
        {
            "op_id": "op-10",
            "kind": "transition",
            "a_candidate_ref": "cand-s1",
            "b_candidate_ref": "cand-s2",
            "style": "dissolve",
            "duration_frames": 12,
        },
        {
            "op_id": "op-11",
            "kind": "music_cue",
            "target_candidate_ref": "cand-s2",
            "asset_ref": "bgm-01",
            "ducking": True,
        },
        {"op_id": "op-12", "kind": "voice_cleanup", "treatment": "voice_isolation"},
        {
            "op_id": "op-13",
            "kind": "sfx_cue",
            "target_candidate_ref": "cand-s1",
            "asset_ref": "sfx-whoosh",
            "duration_frames": 18,
        },
        {"op_id": "op-14", "kind": "color_look", "look_ref": "look-natural"},
        {
            "op_id": "op-15",
            "kind": "speed_change",
            "target_candidate_ref": "cand-s2",
            "factor_pct": 150,
            "capability_verified": True,
        },
        {
            "op_id": "op-16",
            "kind": "manual_required",
            "target_candidate_ref": "cand-s1",
            "effect_note": "film grain overlay",
        },
    ]
    plan = _plan(ops)
    restored = CreativeEditPlanProposalV2.model_validate(plan.model_dump(mode="json"))
    assert restored == plan
    assert len(restored.operations) == 16
    kinds = {type(op).__name__ for op in restored.operations}
    assert kinds == {
        "PlacePrimaryClipOp",
        "BRollOverlayOp",
        "CutawayOp",
        "SplitEditOp",
        "SubtitleTrackOp",
        "TitleCardOp",
        "PunchInOp",
        "PlaceStillOp",
        "TransitionOp",
        "MusicCueOp",
        "VoiceCleanupOp",
        "SfxCueOp",
        "ColorLookOp",
        "SpeedChangeOp",
        "ManualRequiredOp",
    }


def test_compiled_ir_round_trips(compiled: SimpleNamespace) -> None:
    ir = compiled.ir
    assert ir.schema_version == "timeline-ir-v2"
    assert ir.episode_id == EPISODE_ID
    restored = TimelineIrV2.model_validate(ir.model_dump(mode="json"))
    assert restored == ir


# ------------------------------------------------- (d) typed rejections


def test_unknown_op_kind_is_rejected() -> None:
    with pytest.raises(ValidationError, match="explode"):
        _plan([{"op_id": "op-x", "kind": "explode", "candidate_ref": "cand-s1"}])


def test_speed_change_without_verified_capability_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _plan(
            [
                {
                    "op_id": "op-s",
                    "kind": "speed_change",
                    "target_candidate_ref": "cand-s1",
                    "factor_pct": 150,
                    "capability_verified": False,
                }
            ]
        )


def test_plan_and_ir_reject_resolve_fields() -> None:
    with pytest.raises(ValidationError, match="resolve"):
        _plan(
            [
                {
                    "op_id": "op-r",
                    "kind": "place_primary_clip",
                    "candidate_ref": "cand-s1",
                    "resolve_track_index": 2,
                }
            ]
        )
    payload = _primary_only_ir_payload()
    payload["video_tracks"][0]["items"][0]["resolve_clip_id"] = "r1"  # type: ignore[index]
    with pytest.raises(ValidationError, match="resolve"):
        TimelineIrV2.model_validate(payload)


def test_unknown_candidate_ref_is_typed_error() -> None:
    selection = _selection(_candidate("cand-s1", 50, 120))
    plan = _plan([{"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "cand-ghost"}])
    with pytest.raises(CompileIrV2Error) as error:
        compile_ir_v2(selection, plan, source_facts=_facts())
    assert error.value.code == "unknown-candidate"
    assert "cand-ghost" in error.value.detail


def test_episode_and_selection_linkage_enforced() -> None:
    selection = _selection(_candidate("cand-s1", 50, 120), episode_id="ep-t")
    wrong_episode = _plan(
        [{"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "cand-s1"}],
        episode_id="ep-other",
    )
    with pytest.raises(CompileIrV2Error) as episode_error:
        compile_ir_v2(selection, wrong_episode, source_facts=_facts())
    assert episode_error.value.code == "episode-mismatch"

    wrong_selection = CreativeEditPlanProposalV2.model_validate(
        {
            "schema_version": "creative-edit-plan-v2",
            "proposal_id": "cep-t",
            "episode_id": "ep-t",
            "selection_ref": {"proposal_id": "msel-somewhere-else"},
            "operations": [
                {"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "cand-s1"}
            ],
        }
    )
    with pytest.raises(CompileIrV2Error) as selection_error:
        compile_ir_v2(selection, wrong_selection, source_facts=_facts())
    assert selection_error.value.code == "selection-mismatch"


def test_empty_primary_plan_is_typed_error() -> None:
    selection = _selection(_candidate("cand-s1", 50, 120))
    plan = _plan([{"op_id": "op-m", "kind": "music_cue", "target_candidate_ref": None}])
    with pytest.raises(CompileIrV2Error) as error:
        compile_ir_v2(selection, plan, source_facts=_facts())
    assert error.value.code == "empty-primary"


# ------------------------------------------- (b) Decision ID linkage


def test_every_placed_span_carries_known_candidate_ref(compiled: SimpleNamespace) -> None:
    known = {c.candidate_id for c in compiled.selection.candidates}
    placed_refs = {item.candidate_ref for track in compiled.ir.video_tracks for item in track.items}
    placed_refs |= {cue.candidate_ref for cue in compiled.ir.subtitle_cues}
    placed_refs |= {
        item.candidate_ref
        for track in compiled.ir.audio_tracks
        for item in track.items
        if item.candidate_ref is not None
    }
    assert placed_refs
    assert placed_refs <= known


def test_kept_candidates_are_placed_or_consumed(compiled: SimpleNamespace) -> None:
    keeps = {c.candidate_id for c in compiled.selection.candidates if c.intent == "keep"}
    placed_on_primary = {
        item.candidate_ref
        for track in compiled.ir.video_tracks
        if track.role == "primary"
        for item in track.items
    }
    overlaid = {
        item.candidate_ref
        for track in compiled.ir.video_tracks
        if track.role != "primary"
        for item in track.items
    }
    assert placed_on_primary | overlaid == keeps


# ------------------------------------------------- (c) track/role layout


def test_track_roles_are_separate(compiled: SimpleNamespace) -> None:
    roles = {track.role for track in compiled.ir.video_tracks}
    assert roles == {"primary", "b_roll", "graphic"}

    b_roll = next(t for t in compiled.ir.video_tracks if t.role == "b_roll")
    b_roll_refs = {item.candidate_ref for item in b_roll.items}
    assert b_roll_refs == {"cand-shot-d"}

    audio_roles = {track.role for track in compiled.ir.audio_tracks}
    assert audio_roles == {"dialogue", "music"}

    # Subtitle cues live in their own collection — never as video items.
    assert compiled.ir.subtitle_cues
    assert "subtitle" not in roles


def test_subtitle_cues_come_from_transcript_refs(compiled: SimpleNamespace) -> None:
    cues = {cue.transcript_ref: cue for cue in compiled.ir.subtitle_cues}
    assert set(cues) == {"tr-a1", "tr-c1"}
    # Removed candidate's transcript (tr-e1 on shot-e) yields no cue.
    assert "tr-e1" not in cues
    assert cues["tr-a1"].record_span.start_frame == 10
    assert cues["tr-a1"].record_span.end_frame == 90
    assert cues["tr-a1"].text == "today we review the new camera gear"
    assert cues["tr-c1"].record_span.start_frame == 210
    assert cues["tr-c1"].record_span.end_frame == 290
    assert cues["tr-a1"].candidate_ref == "cand-shot-a"
    assert cues["tr-c1"].candidate_ref == "cand-shot-c"


def test_broll_overlays_stay_inside_anchor_spans(compiled: SimpleNamespace) -> None:
    primary = next(t for t in compiled.ir.video_tracks if t.role == "primary")
    primary_spans = [
        (item.record_span.start_frame, item.record_span.end_frame) for item in primary.items
    ]
    b_roll = next(t for t in compiled.ir.video_tracks if t.role == "b_roll")
    assert len(b_roll.items) == 2
    for item in b_roll.items:
        inside = any(
            start <= item.record_span.start_frame and item.record_span.end_frame <= end
            for start, end in primary_spans
        )
        assert inside


# ------------------------------------------------- (f) source-to-record


def test_primary_contiguous_and_handle_math() -> None:
    c1 = _candidate("cand-c1", 50, 120, handles=MomentHandles(in_frame=5, out_frame=7))
    c2 = _candidate("cand-c2", 200, 260)
    clamped = _candidate("cand-c3", 0, 40, handles=MomentHandles(in_frame=3, out_frame=4))
    selection = _selection(c1, c2, clamped)
    plan = _plan(
        [
            {"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "cand-c1"},
            {"op_id": "op-2", "kind": "place_primary_clip", "candidate_ref": "cand-c2"},
            {"op_id": "op-3", "kind": "place_primary_clip", "candidate_ref": "cand-c3"},
        ]
    )
    ir = compile_ir_v2(selection, plan, source_facts=_facts())
    primary = next(t for t in ir.video_tracks if t.role == "primary")
    items = primary.items
    assert [i.record_span.start_frame for i in items] == [0, 82, 142]
    assert [i.record_span.end_frame for i in items] == [82, 142, 186]
    # Handle math: placed source spans include in/out handles, clamped at 0.
    assert items[0].source.span.start_frame == 45
    assert items[0].source.span.end_frame == 127
    assert items[2].source.span.start_frame == 0
    assert items[2].source.span.end_frame == 44
    # Source-to-record is length-preserving at native rate.
    for track in ir.video_tracks:
        for item in track.items:
            assert item.record_span.length == item.source.span.length


def test_ir_model_rejects_gapped_primary() -> None:
    good = TimelineIrV2.model_validate(_primary_only_ir_payload())
    assert good.video_tracks[0].items[-1].record_span.end_frame == 130
    payload = _primary_only_ir_payload()
    payload["video_tracks"][0]["items"][1]["record_span"]["start_frame"] = 75
    with pytest.raises(ValidationError, match="primary_gap"):
        TimelineIrV2.model_validate(payload)


def _primary_only_ir_payload() -> dict[str, Any]:
    selection = _selection(_candidate("cand-p1", 50, 120), _candidate("cand-p2", 200, 260))
    plan = _plan(
        [
            {"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "cand-p1"},
            {"op_id": "op-2", "kind": "place_primary_clip", "candidate_ref": "cand-p2"},
        ]
    )
    ir = compile_ir_v2(selection, plan, source_facts=_facts())
    return ir.model_dump(mode="json")


# ------------------------------------------- transitions + split edits


def test_transition_and_split_edit_compile() -> None:
    c1 = _candidate("cand-s1", 50, 120)
    c2 = _candidate("cand-s2", 200, 260)
    selection = _selection(c1, c2)
    plan = _plan(
        [
            {"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "cand-s1"},
            {"op_id": "op-2", "kind": "place_primary_clip", "candidate_ref": "cand-s2"},
            {
                "op_id": "op-t",
                "kind": "transition",
                "a_candidate_ref": "cand-s1",
                "b_candidate_ref": "cand-s2",
                "style": "dissolve",
                "duration_frames": 12,
            },
            {
                "op_id": "op-j",
                "kind": "split_edit",
                "style": "j_cut",
                "a_candidate_ref": "cand-s1",
                "b_candidate_ref": "cand-s2",
                "lead_frames": 10,
            },
        ]
    )
    ir = compile_ir_v2(selection, plan, source_facts=_facts())
    assert len(ir.transitions) == 1
    transition = ir.transitions[0]
    assert transition.at_record_frame == 70
    assert transition.style == "dissolve"
    assert transition.a_item_ref == "itm-cand-s1"
    assert transition.b_item_ref == "itm-cand-s2"

    dialogue = next(t for t in ir.audio_tracks if t.role == "dialogue")
    spans = [(i.record_span.start_frame, i.record_span.end_frame) for i in dialogue.items]
    assert spans == [(0, 60), (60, 130)]  # J-cut: audio boundary leads video by 10
    sources = [(i.source.span.start_frame, i.source.span.end_frame) for i in dialogue.items]
    assert sources == [(50, 110), (190, 260)]


def test_l_cut_beyond_source_material_is_typed_error() -> None:
    c1 = _candidate("cand-s1", 50, 120)
    c2 = _candidate("cand-s2", 200, 260)
    selection = _selection(c1, c2)
    plan = _plan(
        [
            {"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "cand-s1"},
            {"op_id": "op-2", "kind": "place_primary_clip", "candidate_ref": "cand-s2"},
            {
                "op_id": "op-l",
                "kind": "split_edit",
                "style": "l_cut",
                "a_candidate_ref": "cand-s1",
                "b_candidate_ref": "cand-s2",
                "lead_frames": 100,
            },
        ]
    )
    with pytest.raises(CompileIrV2Error) as error:
        compile_ir_v2(selection, plan, source_facts=_facts(duration=150))
    assert error.value.code == "split-edit-material"


def test_transition_between_non_adjacent_candidates_is_typed_error() -> None:
    c1 = _candidate("cand-s1", 50, 120)
    c2 = _candidate("cand-s2", 200, 260)
    c3 = _candidate("cand-s3", 300, 360)
    selection = _selection(c1, c2, c3)
    plan = _plan(
        [
            {"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "cand-s1"},
            {"op_id": "op-2", "kind": "place_primary_clip", "candidate_ref": "cand-s2"},
            {"op_id": "op-3", "kind": "place_primary_clip", "candidate_ref": "cand-s3"},
            {
                "op_id": "op-t",
                "kind": "transition",
                "a_candidate_ref": "cand-s1",
                "b_candidate_ref": "cand-s3",
                "style": "dissolve",
                "duration_frames": 12,
            },
        ]
    )
    with pytest.raises(CompileIrV2Error) as error:
        compile_ir_v2(selection, plan, source_facts=_facts())
    assert error.value.code == "transition-boundary"


# ------------------------------------------------- (e) determinism


def test_double_compile_is_byte_identical(compiled: SimpleNamespace) -> None:
    first = compile_ir_v2(compiled.selection, compiled.plan, source_facts=compiled.facts)
    assert first == compiled.ir
    assert canonical_model_bytes(first) == canonical_model_bytes(compiled.ir)


# ------------------------------------------------- upgrade mapping


def test_upgrade_from_lite_draft_maps_all_intents(compiled: SimpleNamespace) -> None:
    kinds = [type(op).__name__ for op in compiled.plan.operations]
    assert kinds.count("PlacePrimaryClipOp") == 4  # a, b, c, f (d consumed by overlays)
    assert kinds.count("BRollOverlayOp") == 2
    assert kinds.count("SubtitleTrackOp") == 2
    assert kinds.count("TitleCardOp") == 1
    assert kinds.count("MusicCueOp") == 1
    title = next(op for op in compiled.plan.operations if isinstance(op, TitleCardOp))
    assert title.variant == "lower_third"
    assert title.target_candidate_ref == "cand-shot-a"


def test_presentation_intent_refs_flow_into_ir(compiled: SimpleNamespace) -> None:
    assert compiled.plan.presentation_intents[0].intent_id == "pi-001"
    assert compiled.ir.presentation_intent_refs == ("pi-001",)
