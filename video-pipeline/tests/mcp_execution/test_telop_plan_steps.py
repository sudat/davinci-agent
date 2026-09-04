"""WBS-3 telop plan leg: payload assembly + runner dispatch.

DESIGN telop-nested §8 WBS-3 — the ``apply_subtitles`` leg's mirror: the
committed telop cards must ride ONE ``apply_telop`` step through the same
envelope invariants (``step_from``), the compiler must emit the leg only
when cards exist, and the runner dispatch table must execute it with the
complete committed card set as the gate (Task 8 precedent: a count-only
gate drops evidence the handler actually returns).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from pydantic import ValidationError

from services.contracts.primitives import RationalFrameRate, RecordFrameSpan
from services.creative_plan.audio_finishing import (
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
from services.creative_plan.edit_models_v2 import CreativeEditPlanProposalV2
from services.creative_plan.presentation_intents import load_default_profile
from services.creative_plan.subtitle_models import AsrSegmentV1
from services.creative_plan.subtitle_plan import build_subtitle_plan
from services.editorial_v2.moment_models import (
    MomentCandidateV2,
    MomentProvenance,
    MomentSelectionProposalV2,
    MomentSourceSpan,
)
from services.job_runner.stage_runner import stage_resource
from services.job_runner.stage_runner_models import SequenceClock
from services.job_runner.state_store import StateStore
from services.mcp_client.call_models import McpCallRecorder
from services.mcp_execution.compiler import compile_execution_plan
from services.mcp_execution.placement_steps import telop_step
from services.mcp_execution.plan_models import (
    McpExecutionPlanV1,
    compute_plan_id,
)
from services.mcp_execution.plan_payloads import (
    PrepareProjectParams,
    ProjectReadback,
    TelopCardPayload,
    TelopParams,
)
from services.mcp_execution.readback import verify_readback
from services.mcp_execution.runner import McpExecutionRunnerV2, RetryPolicy
from services.mcp_execution.step_builders import step_from
from tests.editorial_v2.fixtures.three_pass_fixture import EPISODE_ID, SOURCE_ID

RATE = RationalFrameRate(num=30, den=1)


def _card(
    kind: Literal["opening", "persistent", "chapter"], start: int, end: int
) -> TelopCardPayload:
    return TelopCardPayload(
        card_id=f"telop-{kind}",
        kind=kind,
        text="【人生終わった】カメラのケースが見つからない件【ヤバい怒られる】",
        record_span=RecordFrameSpan(start_frame=start, end_frame=end),
    )


def _cards() -> tuple[TelopCardPayload, ...]:
    return (
        _card("opening", 0, 90),
        _card("persistent", 90, 7837),
        _card("chapter", 1632, 1677),
    )


def _handler_rows(cards: tuple[TelopCardPayload, ...]) -> dict[str, list[dict[str, object]]]:
    """The live handler's per-card evidence shape (committed fields only)."""
    return {
        "cards": [
            {
                "card_id": card.card_id,
                "kind": card.kind,
                "text": card.text,
                "record_span": {
                    "start_frame": card.record_span.start_frame,
                    "end_frame": card.record_span.end_frame,
                },
                "spans": [
                    {
                        "start_frame": card.record_span.start_frame,
                        "end_frame": card.record_span.end_frame,
                    }
                ],
                "style": {"font": "Hiragino Sans W6"},
            }
            for card in cards
        ]
    }


# ---------------------------------------------------- (a) payload assembly


def test_telop_step_assembles_the_committed_payload() -> None:
    # Given: the committed telop cards for the representative episode line
    # When: building the telop leg
    # Then: one apply_telop step on the WBS-2 surface with the cards
    #       verbatim, the profile-sourced style reference, the complete
    #       card set as the readback gate, and subtitle-leg envelope shape
    cards = _cards()
    step = telop_step(cards)
    assert step.step_id == "stp-telop-plan"
    assert step.action == "apply_telop"
    assert step.tool_surface == "telop_generation_probe"
    assert isinstance(step.normalized_params, TelopParams)
    assert step.normalized_params.action == "apply_telop"
    assert list(step.normalized_params.cards) == list(cards)
    assert step.normalized_params.style_profile_id == (load_default_profile().telop_style.recipe_id)
    assert step.normalized_params.style_profile_id == "telop/default"
    assert step.expected_readback.kind == "telop_cards"
    assert list(step.expected_readback.cards) == list(cards)
    assert step.rung == "mcp_verified_workflow"
    assert step.retry_class == "transient"
    assert step.preconditions.project_ready
    assert step.preconditions.timeline_ready
    assert step.record_position == 0


# --------------------------------------------------------- (b) compiler leg


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


def _facts() -> SourceFactsV2:
    def transcript(seg: str, text: str, start: int, end: int) -> TranscriptFactV2:
        return TranscriptFactV2(
            segment_id=seg, source_id=SOURCE_ID, text=text, start_frame=start, end_frame=end
        )

    return SourceFactsV2(
        rate=RATE,
        sources=(SourceFactV2(source_id=SOURCE_ID, duration_frames=610),),
        transcripts=(
            transcript("tr-a1", "today we review the new camera gear", 10, 90),
            transcript("tr-c1", "the camera weighs only 400 grams", 210, 290),
        ),
    )


def _ir():
    selection = MomentSelectionProposalV2(
        proposal_id="msel-ep-w3",
        episode_id=EPISODE_ID,
        candidates=(_candidate("cand-s1", 50, 120),),
    )
    plan31 = CreativeEditPlanProposalV2.model_validate(
        {
            "schema_version": "creative-edit-plan-v2",
            "proposal_id": "cep-ep-w3",
            "episode_id": EPISODE_ID,
            "selection_ref": {"proposal_id": "msel-ep-w3"},
            "operations": [
                {"op_id": "op-1", "kind": "place_primary_clip", "candidate_ref": "cand-s1"}
            ],
        }
    )
    return compile_ir_v2(selection, plan31, source_facts=_facts())


@pytest.fixture(scope="module")
def context() -> SimpleNamespace:
    ir = _ir()
    subtitle_plan = build_subtitle_plan(
        (
            AsrSegmentV1(
                segment_id="tr-a1",
                source_id=SOURCE_ID,
                text="today we review the new camera gear",
                start_seconds=10 / 30,
                end_seconds=90 / 30,
            ),
        ),
        source_facts=_facts(),
        ir_v2=ir,
    )
    audio_plan = build_audio_plan(
        AudioFactsV1(
            episode_id=EPISODE_ID,
            dialogue_clean=False,
            has_bgm=True,
            has_ambience=True,
            measured_loudness_ok=False,
        ),
        policy=DEFAULT_AUDIO_POLICY,
        op_requests=(AudioOpRequestV1(op="voice_isolation"),),
    )
    color_plan = build_color_plan(
        ColorFactsV1(
            episode_id=EPISODE_ID,
            exposure_issues=(ColorIssueV1(source_id=SOURCE_ID, detail="underexposed intro"),),
            cameras=(),
            look_configured=True,
            skin_tone_relevant=True,
        ),
        policy=ColorPlanPolicy(color_grade_status="accepted", advanced_qc_status="accepted"),
    )
    return SimpleNamespace(ir=ir, subtitle=subtitle_plan, audio=audio_plan, color=color_plan)


def _compile(context: SimpleNamespace, telop_cards: tuple[TelopCardPayload, ...]):
    return compile_execution_plan(
        context.ir,
        subtitle_plan=context.subtitle,
        audio_plan=context.audio,
        color_plan=context.color,
        presentation_intents=(),
        kit_selections={},
        telop_cards=telop_cards,
    )


def test_compiler_emits_the_telop_leg_only_when_cards_exist(context: SimpleNamespace) -> None:
    # Given: the committed context with and without telop cards
    # When: compiling both plans
    # Then: exactly one apply_telop step rides the plan when cards exist
    #       (after the subtitle leg) and none when the tuple is empty
    with_cards = _compile(context, _cards())
    telop_steps = [s for s in with_cards.steps if s.action == "apply_telop"]
    assert len(telop_steps) == 1
    assert isinstance(telop_steps[0].normalized_params, TelopParams)
    assert list(telop_steps[0].normalized_params.cards) == list(_cards())
    ranks = {s.action: i for i, s in enumerate(with_cards.steps)}
    assert ranks["apply_subtitles"] < ranks["apply_telop"]

    without_cards = _compile(context, ())
    assert not [s for s in without_cards.steps if s.action == "apply_telop"]


def test_telop_cards_reject_plain_dict_inputs_at_the_boundary(
    context: SimpleNamespace,
) -> None:
    # Given: untyped card dicts (a plan builder that skipped the boundary)
    # When: compiling with them
    # Then: the typed payload refuses them (parse, don't validate)
    with pytest.raises(ValidationError):
        compile_execution_plan(
            context.ir,
            subtitle_plan=context.subtitle,
            audio_plan=context.audio,
            color_plan=context.color,
            presentation_intents=(),
            kit_selections={},
            telop_cards=cast("tuple[TelopCardPayload, ...]", ({"kind": "opening"},)),
        )


# ------------------------------------------------------- (c) runner + readback


class FakeExecutor:
    """Replays one canned actual keyed by action; records the dispatch."""

    def __init__(self, script: dict[str, object]) -> None:
        self._script = script
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def __call__(
        self,
        tool_name: str,
        action: str,
        normalized_params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        self.calls.append((tool_name, action, dict(normalized_params)))
        return self._script[action]


def _runner(tmp_path: Path, store: StateStore) -> McpExecutionRunnerV2:
    backends = tmp_path / "backends.json"
    backends.write_text(
        json.dumps(
            {
                "schema_version": "backends-v1",
                "execution_backend": "mcp",
                "analysis_backend": "legacy_local",
                "editorial_contract": "phase1_v1",
            }
        ),
        encoding="utf-8",
    )
    return McpExecutionRunnerV2(
        store=store,
        job_id="dev-job-telop",
        stage_name="stage-finish",
        holder_token="holder-1",  # noqa: S106 (lease token, not a credential)
        ledger_dir=tmp_path / "ledger",
        backends_path=backends,
        clock=SequenceClock(1000),
        retry_policy=RetryPolicy(max_attempts=2),
    )


def test_runner_dispatches_apply_telop_and_verifies_the_card_set(
    tmp_path: Path,
) -> None:
    # Given: a bootstrap + apply_telop plan and the handler-shaped actual
    # When: the runner executes it serially under the lease
    # Then: the dispatch reaches the telop surface, the readback gate
    #       matches the complete committed card set, and every step completes
    cards = _cards()
    step = telop_step(cards)
    bootstrap = step_from(
        "stp-prepare-ep-w3",
        PrepareProjectParams(
            action="prepare_project",
            timeline_name="ep-w3-timeline",
            fps_num=30,
            fps_den=1,
        ),
        ProjectReadback(kind="project", project_name="ep-w3-timeline", timeline_frame_rate="30"),
        "prepare_project",
        "mcp_verified_workflow",
        None,
        -2,
    )
    plan = McpExecutionPlanV1(
        schema_version="mcp-execution-plan-v1",
        plan_id=compute_plan_id(EPISODE_ID, (bootstrap, step)),
        episode_id=EPISODE_ID,
        steps=(bootstrap, step),
    )
    executor = FakeExecutor(
        {
            "prepare_project": {
                "project_name": "ep-w3-timeline",
                "timeline_frame_rate": "30",
            },
            "apply_telop": _handler_rows(cards),
        }
    )
    with StateStore.open(tmp_path / "state.sqlite3") as store:
        store.acquire_lease(
            resource=stage_resource("dev-job-telop", "stage-finish"),
            holder="holder-1",
            now=1000,
            ttl_seconds=60,
        )
        runner = _runner(tmp_path, store)
        report = runner.execute(
            plan,
            executor=executor,
            recorder=McpCallRecorder(
                provider_version="2.98.3",
                resolve_version="21.0.4",
                server_mode="test",
                clock=lambda: 0,
            ),
        )
    assert report.outcome == "completed"
    assert all(s.status == "completed" for s in report.steps)
    assert report.steps[1].failure_code is None
    telop_call = next(c for c in executor.calls if c[1] == "apply_telop")
    assert telop_call[0] == "telop_generation_probe"
    sent = telop_call[2]
    assert sent["style_profile_id"] == "telop/default"
    assert len(cast("list[object]", sent["cards"])) == 3


def test_telop_readback_flags_drifted_text_and_card_count() -> None:
    # Given: the expected telop card set readback
    # When: verifying against drifted actuals
    # Then: text drift and count drift are named mismatches, never passes
    cards = _cards()
    expected = telop_step(cards).expected_readback
    drifted = _handler_rows(cards)
    drifted["cards"][1]["text"] = "drifted テロップ"
    verification = verify_readback(expected, drifted)
    assert verification.matched is False
    assert any("text" in m for m in verification.mismatches)

    short = _handler_rows(cards[:2])
    count_verification = verify_readback(expected, short)
    assert count_verification.matched is False
    assert any("3 cards" in m for m in count_verification.mismatches)
