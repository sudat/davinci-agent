"""McpExecutionPlanV1 compiler tests (task 38).

Given a REAL task-31 IR v2 (Director v2 three-pass over the shared W3
fixture, upgraded and compiled) plus the task-33/34/35 plan artifacts and a
task-37 kit selection, the compiler must produce a complete, deterministic
McpExecutionPlanV1. Locked behaviors:

(a) determinism — double compile is byte-identical with the same plan_id;
(b) coverage — every IR element and plan stage maps to >= 1 step;
(c) unknown intent/op — dangling presentation refs and escape-hatch step
    actions are typed compile/model errors;
(d) kit gate — an accepted_only recipe binding on a not-accepted capability
    refuses the step (typed compile error);
(e) destructive steps always carry an expected readback and a retry class;
(f) static surface — no raw-call constructor exists (closed vocabulary);
(g) round-trip — the plan survives canonical JSON re-validation.

Failed capabilities (real matrix) map to EXPLICIT fallback rungs with a
fallback record — the plan stays complete and the downgrade is reported
(PRD 19), never silent.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import services.mcp_execution.compiler as compiler_module
import services.mcp_execution.plan_models as models_module
from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.creative_plan.audio_finishing import (
    AUDIO_LADDER,
    DEFAULT_AUDIO_POLICY,
    AudioFactsV1,
    AudioOpRequestV1,
    build_audio_plan,
)
from services.creative_plan.color_finishing import (
    ColorFactsV1,
    ColorIssueV1,
    ColorPlanPolicy,
    build_color_plan,
)
from services.creative_plan.compile_ir_v2 import (
    SourceFactsV2,
    SourceFactV2,
    TranscriptFactV2,
    compile_ir_v2,
)
from services.creative_plan.edit_models_v2 import (
    CreativeEditPlanProposalV2,
    SelectionRefV2,
    upgrade_from_creative_draft,
)
from services.creative_plan.ir_models_v2 import PlacedClipV2, TimelineIrV2, VideoTrackV2
from services.creative_plan.presentation_intents import (
    PresentationIntentV2,
    PunchInParams,
    attach_presentation_intents,
)
from services.creative_plan.subtitle_models import AsrSegmentV1
from services.creative_plan.subtitle_plan import build_subtitle_plan
from services.editorial_v2.director_v2 import DirectorV2
from services.editorial_v2.moment_models import (
    MomentCandidateV2,
    MomentProvenance,
    MomentSelectionProposalV2,
    MomentSourceSpan,
)
from services.foundation_io import canonical_model_bytes
from services.mcp_execution.compiler import (
    CompileExecutionPlanError,
    compile_execution_plan,
)
from services.mcp_execution.placement_steps import video_steps
from services.mcp_execution.plan_models import (
    McpExecutionPlanV1,
    McpExecutionStepV1,
)
from services.mcp_execution.plan_payloads import (
    PlaceClipParams,
    PlacementReadback,
    PlaceOverlayParams,
    SubtitleCuesReadback,
)
from services.mcp_execution.step_builders import CapabilityView
from services.production_kit.recipe_select import select_recipe
from services.production_kit.registry import load_kit
from tests.editorial_v2.fixtures.three_pass_fixture import (
    EPISODE_ID,
    SOURCE_ID,
    make_brief,
    make_episode_artifact,
    open_api,
)

RATE_30 = RationalFrameRate(num=30, den=1)


def _candidate(cid: str, start: int, end: int) -> MomentCandidateV2:
    return MomentCandidateV2(
        candidate_id=cid,
        candidate_type="speech",
        source_span=MomentSourceSpan(start_frame=start, end_frame=end),
        intent="keep",
        rationale="synthetic test candidate",
        evidence_refs=(f"shot-{cid}",),
        confidence=0.8,
        provenance=MomentProvenance(producer="test", version="1"),
    )


def _selection(*candidates: MomentCandidateV2) -> MomentSelectionProposalV2:
    return MomentSelectionProposalV2(
        proposal_id="msel-ep-w3",
        episode_id=EPISODE_ID,
        candidates=candidates,
    )


def _plan31(ops: list[dict[str, object]]) -> CreativeEditPlanProposalV2:
    return CreativeEditPlanProposalV2.model_validate(
        {
            "schema_version": "creative-edit-plan-v2",
            "proposal_id": "cep-ep-w3",
            "episode_id": EPISODE_ID,
            "selection_ref": {"proposal_id": "msel-ep-w3"},
            "operations": ops,
        }
    )


def _facts() -> SourceFactsV2:
    def transcript(seg: str, text: str, start: int, end: int) -> TranscriptFactV2:
        return TranscriptFactV2(
            segment_id=seg, source_id=SOURCE_ID, text=text, start_frame=start, end_frame=end
        )

    return SourceFactsV2(
        rate=RATE_30,
        sources=(SourceFactV2(source_id=SOURCE_ID, duration_frames=610),),
        transcripts=(
            transcript("tr-a1", "today we review the new camera gear", 10, 90),
            transcript("tr-c1", "the camera weighs only 400 grams", 210, 290),
            transcript("tr-e1", "so anyway i kept walking around the park", 510, 590),
        ),
    )


def _audio_plan():
    return build_audio_plan(
        AudioFactsV1(
            episode_id=EPISODE_ID,
            dialogue_clean=False,
            has_bgm=True,
            has_ambience=True,
            measured_loudness_ok=False,
        ),
        policy=DEFAULT_AUDIO_POLICY,
        op_requests=(
            AudioOpRequestV1(op="voice_isolation"),
            AudioOpRequestV1(op="eq", justification="room resonance needs correction"),
        ),
    )


def _color_plan():
    return build_color_plan(
        ColorFactsV1(
            episode_id=EPISODE_ID,
            exposure_issues=(ColorIssueV1(source_id=SOURCE_ID, detail="underexposed intro"),),
            cameras=(),
            look_configured=True,
            skin_tone_relevant=True,
        ),
        policy=ColorPlanPolicy(color_grade_status="accepted", advanced_qc_status="accepted"),
    )


def _compile(ir, *, subtitle_plan, intents, kit_selections):
    return compile_execution_plan(
        ir,
        subtitle_plan=subtitle_plan,
        audio_plan=_audio_plan(),
        color_plan=_color_plan(),
        presentation_intents=intents,
        kit_selections=kit_selections,
    )


@pytest.fixture(scope="module")
def compiled(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """Full happy-path fixture: real Director v2 IR + all four plan inputs."""
    tmp = tmp_path_factory.mktemp("task38-fixture")
    with open_api(make_episode_artifact(), tmp) as api:
        result = DirectorV2().run_three_pass(make_brief(), api)
    selection = result.moment_selection.proposal
    operations = upgrade_from_creative_draft(
        result.creative_edit, selection, b_roll_matches=result.moment_selection.b_roll_matches
    )
    plan31 = CreativeEditPlanProposalV2(
        schema_version="creative-edit-plan-v2",
        proposal_id="cep-ep-w3",
        episode_id=selection.episode_id,
        selection_ref=SelectionRefV2(proposal_id=selection.proposal_id),
        operations=operations,
    )
    ir_base = compile_ir_v2(selection, plan31, source_facts=_facts())
    intent = PresentationIntentV2(
        intent_id="pi-001",
        kind="emphasis_punch_in",
        target_span=RecordFrameSpan(start_frame=0, end_frame=60),
        params=PunchInParams(scale=1.4, duration_frames=30),
        rationale="emphasize the opening hook",
    )
    ir = attach_presentation_intents(ir_base, (intent,))
    subtitle_plan = build_subtitle_plan(
        (
            AsrSegmentV1(
                segment_id="tr-a1",
                source_id=SOURCE_ID,
                text="today we review the new camera gear",
                start_seconds=10 / 30,
                end_seconds=90 / 30,
            ),
            AsrSegmentV1(
                segment_id="tr-c1",
                source_id=SOURCE_ID,
                text="the camera weighs only 400 grams",
                start_seconds=210 / 30,
                end_seconds=290 / 30,
            ),
        ),
        source_facts=_facts(),
        ir_v2=ir_base,
    )
    kit_selection = select_recipe(load_kit(), "emphasis_punch_in")
    plan = _compile(
        ir,
        subtitle_plan=subtitle_plan,
        intents=(intent,),
        kit_selections={"emphasis_punch_in": kit_selection},
    )
    return SimpleNamespace(
        ir=ir, intent=intent, subtitle_plan=subtitle_plan, plan=plan, selection=selection
    )


# --------------------------------------------------------- (a) determinism


@pytest.fixture(scope="module")
def effect_case() -> SimpleNamespace:
    """Hand-built IR exercising effects, transitions, and contradictions."""
    ops: list[dict[str, object]] = [
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
            "op_id": "op-p",
            "kind": "punch_in",
            "target_candidate_ref": "cand-s2",
            "mode": "punch_in",
        },
        {
            "op_id": "op-v",
            "kind": "speed_change",
            "target_candidate_ref": "cand-s2",
            "factor_pct": 150,
            "capability_verified": True,
        },
        {
            "op_id": "op-m",
            "kind": "manual_required",
            "target_candidate_ref": "cand-s1",
            "effect_note": "film grain overlay",
        },
        {
            "op_id": "op-mu",
            "kind": "music_cue",
            "target_candidate_ref": "cand-s2",
            "asset_ref": "bgm-01",
            "ducking": True,
        },
        {"op_id": "op-cl", "kind": "color_look", "look_ref": "look-x"},
    ]
    ir = compile_ir_v2(
        _selection(_candidate("cand-s1", 50, 120), _candidate("cand-s2", 200, 260)),
        _plan31(ops),
        source_facts=_facts(),
    )
    # no look configured in the color plan -> the color_look intent must land
    # on manual finalization (contradiction is reported, never silently dropped)
    no_look_color = build_color_plan(
        ColorFactsV1(episode_id=EPISODE_ID),
        policy=ColorPlanPolicy(color_grade_status="accepted", advanced_qc_status="accepted"),
    )
    plan = compile_execution_plan(
        ir,
        subtitle_plan=build_subtitle_plan((), source_facts=_facts(), ir_v2=ir),
        audio_plan=_audio_plan(),
        color_plan=no_look_color,
        presentation_intents=(),
        kit_selections={},
    )
    return SimpleNamespace(ir=ir, plan=plan)


def test_double_compile_is_byte_identical_with_same_plan_id(compiled: SimpleNamespace) -> None:
    again = _compile(
        compiled.ir,
        subtitle_plan=compiled.subtitle_plan,
        intents=(compiled.intent,),
        kit_selections={"emphasis_punch_in": select_recipe(load_kit(), "emphasis_punch_in")},
    )
    assert canonical_model_bytes(again) == canonical_model_bytes(compiled.plan)
    assert again.plan_id == compiled.plan.plan_id
    assert re.fullmatch(r"[0-9a-f]{64}", compiled.plan.plan_id)


def test_plan_model_rejects_unsorted_or_duplicate_steps(compiled: SimpleNamespace) -> None:
    payload = compiled.plan.model_dump(mode="json")
    payload["steps"] = list(reversed(payload["steps"]))
    with pytest.raises(ValidationError, match="step_order"):
        McpExecutionPlanV1.model_validate(payload)

    payload = compiled.plan.model_dump(mode="json")
    payload["steps"] = [payload["steps"][0], payload["steps"][0]]
    with pytest.raises(ValidationError, match="duplicate_step"):
        McpExecutionPlanV1.model_validate(payload)


# ------------------------------------------------------------ (b) coverage


def test_every_video_item_has_a_placement_step(compiled: SimpleNamespace) -> None:
    items = [i.item_id for t in compiled.ir.video_tracks for i in t.items]
    placed = {
        s.normalized_params.item_id
        for s in compiled.plan.steps
        if hasattr(s.normalized_params, "item_id")
    }
    assert items
    for item_id in items:
        assert item_id in placed, f"video item {item_id} has no placement step"


def test_every_audio_item_has_a_placement_step(compiled: SimpleNamespace) -> None:
    items = [i.item_id for t in compiled.ir.audio_tracks for i in t.items]
    placed = {
        s.normalized_params.item_id
        for s in compiled.plan.steps
        if hasattr(s.normalized_params, "item_id")
    }
    for item_id in items:
        assert item_id in placed, f"audio item {item_id} has no placement step"


def test_subtitle_plan_step_carries_the_committed_cue_set(compiled: SimpleNamespace) -> None:
    """Task 8 repair: the runner gate for the native subtitle mutation is
    the COMPLETE committed cue set (per-cue id/text/record span), never a
    bare count — the count-only readback failed every live attempt with
    `cue_count: missing` because the handler returns exact per-cue rows."""

    subtitle_steps = [
        s for s in compiled.plan.steps if s.normalized_params.action == "apply_subtitles"
    ]
    assert len(subtitle_steps) == 1
    readback = subtitle_steps[0].expected_readback
    assert isinstance(readback, SubtitleCuesReadback)
    assert readback.kind == "subtitle_cues"
    committed = [
        (cue.cue_id, "\n".join(cue.lines), cue.record_span)
        for cue in compiled.subtitle_plan.cues
    ]
    assert [(c.cue_id, c.text, c.record_span) for c in readback.cues] == committed


@pytest.mark.parametrize(
    "stage",
    [
        "dialogue_cleanup",
        "dialogue_level_normalization",
        "optional_eq_compression_voice_isolation",
        "bgm_placement",
        "music_ducking",
        "ambience_preservation",
        "loudness_peak_qc",
    ],
)
def test_every_enabled_audio_stage_has_a_step(compiled: SimpleNamespace, stage: str) -> None:
    step = next(
        (s for s in compiled.plan.steps if getattr(s.normalized_params, "stage", None) == stage),
        None,
    )
    assert step is not None, f"enabled audio stage {stage} has no step"


def test_every_needed_color_section_has_a_grade_step(compiled: SimpleNamespace) -> None:
    needed = {"technical_correction.exposure", "channel_episode_look"}
    graded = {
        s.normalized_params.section
        for s in compiled.plan.steps
        if s.normalized_params.action == "apply_color"
    }
    assert needed <= graded


def test_every_effect_intent_is_covered(effect_case: SimpleNamespace) -> None:
    kinds = [e.kind for e in effect_case.ir.effect_intents]
    assert set(kinds) >= {"punch_in", "speed_change", "manual_required", "ducking", "color_look"}
    covered = {
        s.normalized_params.effect_kind
        for s in effect_case.plan.steps
        if hasattr(s.normalized_params, "effect_kind")
    }
    assert set(kinds) <= covered


def test_color_look_contradiction_lands_on_manual(effect_case: SimpleNamespace) -> None:
    manual = [
        s
        for s in effect_case.plan.steps
        if s.normalized_params.action == "manual_required"
        and s.normalized_params.effect_kind == "color_look"
    ]
    assert len(manual) == 1
    assert manual[0].rung == "manual_finalization"
    assert manual[0].fallback_record is not None


def test_transition_maps_to_explicit_fallback_rung(effect_case: SimpleNamespace) -> None:
    transitions = [
        s for s in effect_case.plan.steps if s.normalized_params.action == "apply_transition"
    ]
    assert len(transitions) == 1
    step = transitions[0]
    assert step.rung == "external_asset_render"
    assert step.tool_surface == "external_asset_builder"
    assert step.fallback_record is not None
    assert step.fallback_record.capability == "transition-path"
    assert step.fallback_record.status == "failed"


def test_presentation_intent_has_kit_recipe_step(compiled: SimpleNamespace) -> None:
    kit_steps = [s for s in compiled.plan.steps if s.normalized_params.action == "apply_kit_recipe"]
    assert len(kit_steps) == 1
    params = kit_steps[0].normalized_params
    assert params.intent_id == "pi-001"
    assert params.kind == "emphasis_punch_in"
    assert params.recipe_id == "motion/punch-in"
    assert params.resolved_params


# --------------------------------------------------- (c) unknown intent/op


def test_dangling_presentation_ref_is_typed_error(compiled: SimpleNamespace) -> None:
    ir = compiled.ir.model_copy(update={"presentation_intent_refs": ("pi-ghost",)})
    with pytest.raises(CompileExecutionPlanError) as error:
        _compile(
            ir,
            subtitle_plan=compiled.subtitle_plan,
            intents=(compiled.intent,),
            kit_selections={},
        )
    assert error.value.code == "unknown-intent"
    assert "pi-ghost" in error.value.detail


def test_step_model_rejects_escape_hatch_action(compiled: SimpleNamespace) -> None:
    payload = compiled.plan.model_dump(mode="json")["steps"][0]
    payload["action"] = "escape_hatch"
    with pytest.raises(ValidationError, match="escape_hatch"):
        McpExecutionStepV1.model_validate(payload)


def test_step_model_rejects_params_action_mismatch(compiled: SimpleNamespace) -> None:
    steps = compiled.plan.model_dump(mode="json")["steps"]
    steps[0]["normalized_params"] = steps[1]["normalized_params"]
    with pytest.raises(ValidationError):
        McpExecutionStepV1.model_validate(steps[0])


# --------------------------------------------------- (d) kit accepted_only


def test_not_accepted_kit_binding_refuses_step(compiled: SimpleNamespace) -> None:
    selection = select_recipe(load_kit(), "emphasis_punch_in")
    doctored = selection.model_copy(
        update={
            "recipe": selection.recipe.model_copy(
                update={
                    "capability_binding": selection.recipe.capability_binding.model_copy(
                        update={"accepted_only": True}
                    )
                }
            )
        }
    )
    statuses = {
        "project-timeline-creation": "accepted",
        "import-media": "accepted",
        "exact-source-range-placement": "accepted",
        "subtitle-capability": "accepted",
        "voice-isolation": "accepted",
        "clip-transform-punch-in": "failed",
        "color-grade-preset-drx": "accepted",
        "advanced-delivery-qc": "accepted",
    }
    with pytest.raises(CompileExecutionPlanError) as error:
        compile_execution_plan(
            compiled.ir,
            subtitle_plan=build_subtitle_plan((), source_facts=_facts(), ir_v2=compiled.ir),
            audio_plan=_audio_plan(),
            color_plan=_color_plan(),
            presentation_intents=(compiled.intent,),
            kit_selections={"emphasis_punch_in": doctored},
            capability_statuses=statuses,
        )
    assert error.value.code == "capability-not-accepted"
    assert "clip-transform-punch-in" in error.value.detail


# -------------------------------------- fallback rungs (PRD 19, explicit)


def test_failed_capabilities_map_to_explicit_fallback_steps(compiled: SimpleNamespace) -> None:
    eq_steps = [s for s in compiled.plan.steps if s.normalized_params.action == "apply_audio_op"]
    assert eq_steps
    for step in eq_steps:
        assert step.rung == "direct_scripting_gap_adapter"
        assert step.fallback_record is not None
        assert step.fallback_record.capability == "audio-property-operation"
        assert step.fallback_record.status == "failed"
    duck_steps = [s for s in compiled.plan.steps if s.normalized_params.action == "apply_ducking"]
    assert duck_steps
    for step in duck_steps:
        assert step.rung == "direct_scripting_gap_adapter"
        assert step.fallback_record is not None
        assert step.fallback_record.capability == "bgm-track-ducking"


def test_accepted_capabilities_stay_on_mcp_rung(compiled: SimpleNamespace) -> None:
    by_action = {s.normalized_params.action: s for s in compiled.plan.steps}
    assert by_action["prepare_project"].rung == "mcp_verified_workflow"
    assert by_action["import_media"].rung == "mcp_verified_workflow"
    assert by_action["place_clip"].rung == "mcp_verified_workflow"
    assert by_action["apply_subtitles"].rung == "mcp_verified_workflow"
    assert by_action["apply_color"].rung == "mcp_verified_workflow"
    voice = [
        s for s in compiled.plan.steps if s.normalized_params.action == "apply_voice_isolation"
    ]
    assert voice
    assert all(s.rung == "mcp_verified_workflow" for s in voice)
    kit = by_action["apply_kit_recipe"]
    assert kit.rung == "mcp_verified_workflow"
    assert kit.fallback_record is None


# --------------------------------------------------- (e) destructive readback


def test_destructive_steps_carry_readback_and_retry_class(compiled: SimpleNamespace) -> None:
    destructive = [s for s in compiled.plan.steps if s.destructive]
    assert destructive
    for step in destructive:
        assert step.expected_readback is not None
        assert step.retry_class in ("transient", "permanent")


# --------------------------------------------------- (f) static surface


def test_module_exposes_no_raw_call_constructor() -> None:
    forbidden = re.compile(r"raw|arbitrary|free_?form|emit_call|call_tool|generic")
    for module in (compiler_module, models_module):
        offenders = [name for name in vars(module) if forbidden.search(name)]
        assert offenders == []
    assert compiler_module.__all__ == [
        "CompileExecutionPlanError",
        "compile_execution_plan",
    ]


# ------------------------------------------------------- (g) round-trip


def test_plan_round_trips_through_canonical_json(compiled: SimpleNamespace) -> None:
    restored = McpExecutionPlanV1.model_validate(compiled.plan.model_dump(mode="json"))
    assert restored == compiled.plan
    assert canonical_model_bytes(restored) == canonical_model_bytes(compiled.plan)


def test_episode_mismatch_is_typed_error(compiled: SimpleNamespace) -> None:
    foreign_ir = compiled.ir.model_copy(update={"episode_id": "ep-x"})
    foreign_subtitle = build_subtitle_plan((), source_facts=_facts(), ir_v2=foreign_ir)
    with pytest.raises(CompileExecutionPlanError) as error:
        compile_execution_plan(
            compiled.ir,
            subtitle_plan=foreign_subtitle,
            audio_plan=_audio_plan(),
            color_plan=_color_plan(),
            presentation_intents=(),
            kit_selections={},
        )
    assert error.value.code == "episode-mismatch"


def test_phase_order_all_placements_before_subtitle_with_multiple_positions() -> None:
    """Regression for Task 5 ordering defect: execution phases are ordered by
    STEP_CLASS_RANK first, record_position second.

    The compiled plan previously sorted by (record_position, STEP_CLASS_RANK),
    so `apply_subtitles` at position 0 ran after only the first audio/video
    placement pair and before remaining placements at later positions.
    Correct phase order keeps ALL placements before subtitles/effects regardless
    of their record positions, while preserving deterministic position+id within
    each phase.
    """

    selection = _selection(_candidate("cand-s1", 0, 90), _candidate("cand-s2", 96, 186))
    ops: list[dict[str, object]] = [
        {"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "cand-s1"},
        {"op_id": "op-2", "kind": "place_primary_clip", "candidate_ref": "cand-s2"},
    ]
    ir = compile_ir_v2(selection, _plan31(ops), source_facts=_facts())
    subtitle_plan = build_subtitle_plan(
        (
            AsrSegmentV1(
                segment_id="tr-a1",
                source_id=SOURCE_ID,
                text="hello",
                start_seconds=0,
                end_seconds=3,
            ),
        ),
        source_facts=_facts(),
        ir_v2=ir,
    )
    plan = compile_execution_plan(
        ir,
        subtitle_plan=subtitle_plan,
        audio_plan=_audio_plan(),
        color_plan=_color_plan(),
        presentation_intents=(),
        kit_selections={},
    )
    actions = [s.action for s in plan.steps]
    # prepare and import remain first
    assert actions.index("prepare_project") < actions.index("import_media")
    assert actions.index("import_media") < actions.index("place_clip")
    # every placement precedes subtitle/effect/audio/color/render regardless of position
    place_actions = {"place_clip", "place_audio", "place_overlay", "place_title"}
    place_indices = [i for i, a in enumerate(actions) if a in place_actions]
    subtitle_idx = actions.index("apply_subtitles")
    assert place_indices, "no placement steps emitted"
    assert max(place_indices) < subtitle_idx, (
        f"subtitle interleaved before later placement: actions={actions} "
        f"place_indices={place_indices} subtitle_idx={subtitle_idx} "
        f"steps={[(s.step_id, s.action, s.record_position) for s in plan.steps]}"
    )
    # deterministic within phase: placements ordered by position then id
    place_steps = [s for s in plan.steps if s.action in place_actions]
    assert place_steps == sorted(
        place_steps, key=lambda s: (s.record_position, s.step_id)
    )


def test_dialogue_only_final_loudness_qc_after_voice_isolation() -> None:
    """Regression for second audio-phase ordering defect: lexical step_id
    placed loudness_peak_qc before voice_isolation, while the committed
    AudioFinishingPlanV1 ladder requires cleanup → normalization →
    voice_isolation → loudness_peak_qc so QC measures the post-voice timeline."""

    selection = _selection(_candidate("cand-s1", 0, 90))
    ir = compile_ir_v2(
        selection,
        _plan31([{"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "cand-s1"}]),
        source_facts=_facts(),
    )
    subtitle_plan = build_subtitle_plan((), source_facts=_facts(), ir_v2=ir)
    audio_plan = build_audio_plan(
        AudioFactsV1(
            episode_id=selection.episode_id,
            dialogue_clean=False,
            has_bgm=False,
            has_ambience=False,
            measured_loudness_ok=False,
        ),
        policy=DEFAULT_AUDIO_POLICY,
        op_requests=(AudioOpRequestV1(op="voice_isolation"),),
    )
    plan = compile_execution_plan(
        ir,
        subtitle_plan=subtitle_plan,
        audio_plan=audio_plan,
        color_plan=_color_plan(),
        presentation_intents=(),
        kit_selections={},
    )
    # sanity: ladder order is cleanup, normalization, optional(voice), loudness
    assert [s.stage for s in audio_plan.stages if s.enabled] == [
        "dialogue_cleanup",
        "dialogue_level_normalization",
        "optional_eq_compression_voice_isolation",
        "loudness_peak_qc",
    ]
    actions = [s.action for s in plan.steps]
    # phase order still holds: placements before subtitles before audio
    place_actions = {"place_clip", "place_audio", "place_overlay", "place_title"}
    assert max(
        i for i, a in enumerate(actions) if a in place_actions
    ) < actions.index("apply_subtitles")
    assert actions.index("apply_subtitles") < actions.index("apply_audio_stage")
    # audio phase must preserve committed stage order; QC never before preceding mutation
    audio_actions = {
        "apply_audio_stage",
        "apply_voice_isolation",
        "apply_audio_op",
        "apply_ducking",
    }
    audio_steps = [s for s in plan.steps if s.action in audio_actions]
    audio_ids = [s.step_id for s in audio_steps]
    # enabled audio ops: only voice_isolation in this dialogue-only fixture
    assert "stp-audioop-voice_isolation" in audio_ids
    assert "stp-audio-loudness_peak_qc" in audio_ids
    voice_idx = audio_ids.index("stp-audioop-voice_isolation")
    qc_idx = audio_ids.index("stp-audio-loudness_peak_qc")
    cleanup_idx = audio_ids.index("stp-audio-dialogue_cleanup")
    norm_idx = audio_ids.index("stp-audio-dialogue_level_normalization")
    assert cleanup_idx < norm_idx < voice_idx < qc_idx, (
        f"audio order must be cleanup → normalization → voice → QC, got {audio_ids}"
    )
    # general property: among enabled audio stages, order follows AUDIO_LADDER
    ladder_pos = {name: i for i, name in enumerate(AUDIO_LADDER)}

    def ladder_of(step):  # type: ignore[no-untyped-def]
        params = step.normalized_params
        stage = getattr(params, "stage", None)
        if getattr(params, "action", None) == "apply_voice_isolation":
            return ladder_pos["optional_eq_compression_voice_isolation"]
        if getattr(params, "action", None) == "apply_audio_op":
            return ladder_pos["optional_eq_compression_voice_isolation"]
        if getattr(params, "action", None) == "apply_ducking":
            return ladder_pos["music_ducking"]
        if isinstance(stage, str):
            return ladder_pos.get(stage, 999)
        return 999

    ladder_indices = [ladder_of(s) for s in audio_steps]
    assert ladder_indices == sorted(ladder_indices), (
        f"audio steps must be ladder-ordered, got "
        f"{[(s.step_id, ladder_of(s)) for s in audio_steps]}"
    )


def test_video_steps_emit_overlay_on_track_two_primary_on_track_one() -> None:
    """The overlay-over-base stacking unlock: overlay-role items compile to
    place_overlay on video track 2 while primary clips stay on track 1, so
    both can occupy the same record span (measured v44-real-01 blocker)."""

    def _clip(item_id: str) -> PlacedClipV2:
        return PlacedClipV2(
            item_id=item_id,
            source=SourceRef(
                source_id="src-001",
                span=SourceFrameSpan(
                    start_frame=10, end_frame=70, rate=RationalFrameRate(num=30, den=1)
                ),
            ),
            record_span=RecordFrameSpan(start_frame=0, end_frame=60),
            candidate_ref="cand-1",
        )

    ir = TimelineIrV2(
        schema_version="timeline-ir-v2",
        episode_id="ep-stack",
        rate=RationalFrameRate(num=30, den=1),
        video_tracks=(
            VideoTrackV2(role="primary", track_id="vt-primary", items=(_clip("itm-base"),)),
            VideoTrackV2(role="still", track_id="vt-theme", items=(_clip("itm-theme"),)),
        ),
    )
    steps = video_steps(ir, CapabilityView({"exact-source-range-placement": "accepted"}, {}))

    clip_steps = [s for s in steps if s.normalized_params.action == "place_clip"]
    overlay_steps = [s for s in steps if s.normalized_params.action == "place_overlay"]
    assert len(clip_steps) == 1
    assert len(overlay_steps) == 1

    overlay_params = overlay_steps[0].normalized_params
    clip_readback = clip_steps[0].expected_readback
    overlay_readback = overlay_steps[0].expected_readback
    assert isinstance(overlay_params, PlaceOverlayParams)
    assert isinstance(clip_readback, PlacementReadback)
    assert isinstance(overlay_readback, PlacementReadback)

    assert "track_index" not in PlaceClipParams.model_fields
    assert overlay_params.track_index == 2
    assert clip_readback.track_index == 1
    assert overlay_readback.track_index == 2
