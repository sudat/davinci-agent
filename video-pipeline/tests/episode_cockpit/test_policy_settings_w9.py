"""W9: the adopted policy's conditional presentation reaches real settings.

Hermetic pattern: the mapper is pure (no IO); the derive test fakes the
solver seams at the episode_runner_selection boundary and captures the
subtitle inputs the real project/build_ir/SRT path consumes; the stage
test reuses the selection-rebuild journal harness so the outcome journal
itself proves the field→setting mapping reloads.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.cli import episode_runner_rebuild, episode_runner_selection
from services.cli.episode_runner_selection import SelectionRerun
from services.cli.real_pool import SpeechSegment
from services.compile.phase0c import build_ir
from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
)
from services.contracts.primitives import Producer, RationalFrameRate, SourceFrameSpan
from services.editorial.prompt import render_adopted_policy_text
from services.episode_cockpit.consultation_store import (
    CONNECTED_POLICY_FIELDS,
    AdoptedPolicyV1,
    ConsultationJudgmentV1,
    ConsultationProposalDetails,
    ConsultationProposalSetV1,
    ConsultationProposalV1,
    ConsultationRecordV1,
    ConsultationScope,
    append_consultation,
    append_judgment,
    append_proposal_set,
    latest_adopted_policy,
    load_policy_outcomes,
    now_stamp,
    policy_summary,
)
from services.episode_cockpit.policy_settings import (
    LOCAL_EXCEPTION_UNADDRESSED,
    derive_policy_settings,
    select_policy_subtitles,
)
from services.plan.edit_plan_models import SelectionPlanRef
from services.plan.planner_models import OrderingTrace, PlannerSolution
from services.preview.srt import expected_subtitle_cues, render_srt
from services.review_command.store import initialize_store, load_head

RATE = RationalFrameRate(num=30, den=1)


def _policy(**overrides: Any) -> AdoptedPolicyV1:
    base: dict[str, Any] = {
        "consultation_id": "c1",
        "judgment_id": "j1",
        "proposal_id": "prop-1",
        "decision": "adopt",
        "scope": ConsultationScope(composition=True, appearance=True, audio=False),
        "audience_message": "初めての人に",
        "structure": "導入→本編→締め",
        "duration_estimate": "約4分",
        "candidate_scenes": ("opening",),
        "subtitle_policy": "場所変更時のみ大テロップ",
        "audio_policy": "BGM小さめ",
        "tempo_policy": "前半重視",
        "reference_mapping": "",
        "unused_reasons": "",
        "unconfirmed": (),
        "note": None,
        "presentation_condition": "normal",
    }
    base.update(overrides)
    return AdoptedPolicyV1.model_validate(base)


def test_legacy_details_without_condition_default_to_normal() -> None:
    details = ConsultationProposalDetails.model_validate(
        {
            "audience_message": "a",
            "structure": "s",
            "duration_estimate": "d",
            "subtitle_policy": "sub",
            "audio_policy": "au",
            "tempo_policy": "t",
            "reference_mapping": "r",
            "unused_reasons": "u",
        }
    )

    assert details.presentation_condition == "normal"


def test_connected_fields_include_presentation_condition() -> None:
    assert "presentation_condition" in tuple(CONNECTED_POLICY_FIELDS)


def test_normal_appearance_maps_to_all_kept_with_trace() -> None:
    record = derive_policy_settings(_policy(presentation_condition="normal"))

    assert record.subtitle_mode == "all_kept"
    assert record.telop_condition == "normal"
    assert record.unaddressed == ()
    row = next(
        entry
        for entry in record.entries
        if entry.setting == "review_subtitle_inclusion=all_kept"
    )
    assert "subtitle_policy" in row.policy_field
    assert select_policy_subtitles(record, ("s1", "s2", "s3", "s4"), {"s1", "s2", "s4"}) == (
        "s1",
        "s2",
        "s4",
    )


def test_location_change_only_selects_run_boundaries() -> None:
    record = derive_policy_settings(_policy(presentation_condition="location_change_only"))

    assert record.subtitle_mode == "boundary_only"
    assert record.unaddressed == ()
    assert select_policy_subtitles(
        record, ("s1", "s2", "s3", "s4", "s5"), {"s1", "s2", "s4", "s5"}
    ) == ("s1", "s4")
    assert select_policy_subtitles(record, ("s1", "s2"), set()) == ()
    assert select_policy_subtitles(record, ("s1",), {"s1"}) == ("s1",)


def test_local_exception_carries_condition_and_lists_unaddressed() -> None:
    record = derive_policy_settings(_policy(presentation_condition="local_exception"))

    assert record.subtitle_mode == "all_kept"
    assert record.telop_condition == "local_exception"
    assert LOCAL_EXCEPTION_UNADDRESSED in tuple(record.unaddressed)
    assert select_policy_subtitles(record, ("s1", "s2"), {"s1", "s2"}) == ("s1", "s2")


def test_unadopted_appearance_keeps_baseline_without_policy_claim() -> None:
    record = derive_policy_settings(
        _policy(scope=ConsultationScope(composition=True, appearance=False, audio=False))
    )

    assert record.subtitle_mode == "all_kept"
    assert record.unaddressed == ()
    subtitle_rows = [entry for entry in record.entries if "subtitle" in entry.setting]
    assert subtitle_rows != []
    assert all("unadopted" in entry.policy_field for entry in subtitle_rows)
    assert not any("subtitle_policy" in entry.policy_field for entry in record.entries)


def test_audio_scope_records_binding_and_unaddressed_detail() -> None:
    record = derive_policy_settings(
        _policy(scope=ConsultationScope(composition=False, appearance=False, audio=True))
    )

    assert any(entry.setting == "review_audio_binding=mirror_video" for entry in record.entries)
    assert any("audio_policy_detail" in item for item in record.unaddressed)


def test_summary_carries_condition_and_prompt_renders_it() -> None:
    summary = policy_summary(_policy(presentation_condition="location_change_only"))

    assert summary.presentation_condition == "location_change_only"
    text = render_adopted_policy_text(summary)
    assert text is not None
    assert "presentation_condition: location_change_only" in text


def _rerun(speech_ids: tuple[str, ...], kept: frozenset[str]) -> SelectionRerun:
    by_id = {sid: f"cand-{sid}" for sid in speech_ids}
    selection = SimpleNamespace(
        episode_id="ep-w9",
        candidates=[
            SimpleNamespace(
                candidate_id=cid, intent="keep" if sid in kept else "drop"
            )
            for sid, cid in by_id.items()
        ],
    )
    reconciled = SimpleNamespace(
        segment_links=[
            SimpleNamespace(segment_id=sid, candidate_ids=(cid,))
            for sid, cid in by_id.items()
        ]
    )
    pool = SimpleNamespace(source_id="src-ep", edit_source_sha="0" * 64, total_frames=90)
    speech = tuple(
        SpeechSegment(
            segment_id=sid, text=f"line-{sid}",
            start_frame=pos * 30, end_frame=pos * 30 + 30,
        )
        for pos, sid in enumerate(speech_ids)
    )
    return SelectionRerun(
        outcome=SimpleNamespace(request_hash="req-w9"),
        selection=selection,  # type: ignore[arg-type]
        reconciled=reconciled,  # type: ignore[arg-type]
        pool=pool,  # type: ignore[arg-type]
        speech=speech,
    )


def _solution() -> PlannerSolution:
    return PlannerSolution(
        episode_id="ep-w9",
        ordering_rule_id="rule-1",
        selected_candidate_ids=(),
        allocated=(),
        total_duration_frames=0,
        scores=(),
        ordering_trace=OrderingTrace(rule_id="rule-1", rule="r"),
    )


def test_derive_policy_plan_filters_subtitles_into_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(episode_runner_selection, "planner_input", lambda *args: None)
    monkeypatch.setattr(episode_runner_selection, "solve", lambda *args: _solution())
    monkeypatch.setattr(
        episode_runner_selection,
        "selection_base_ref",
        lambda episode_root, episode_id: SelectionPlanRef(
            episode_id=episode_id,
            plan_version="v1",
            plan_sha256="0" * 64,
            plan_artifact_id="artifact-1",
        ),
    )
    def fake_generate(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace()

    monkeypatch.setattr(episode_runner_selection, "generate", fake_generate)
    monkeypatch.setattr(episode_runner_selection, "compile_ir", lambda *args, **kwargs: None)

    def fake_project(plan: Any, reconciled: Any, **kwargs: Any) -> Any:
        captured["subtitles"] = kwargs["subtitles"]
        return SimpleNamespace(plan=SimpleNamespace(plan_version="v9"))

    monkeypatch.setattr(episode_runner_selection, "project_plan", fake_project)
    rerun = _rerun(("s1", "s2", "s3", "s4"), frozenset({"s1", "s2", "s4"}))

    episode_runner_selection.derive_policy_plan(
        tmp_path, rerun, _policy(presentation_condition="location_change_only")
    )

    assert [sub.segment_id for sub in captured["subtitles"]] == ["s1", "s4"]


def _subtitle_item(item_id: str, text: str, start: int, end: int) -> EditPlanItem0C:
    return EditPlanItem0C(
        item_id=item_id,
        kind="subtitle",
        source_id="src-ep",
        span=SourceFrameSpan(start_frame=start, end_frame=end, rate=RATE),
        track_index=3,
        subtitle_text=text,
    )


def test_compile_path_consumes_filtered_subtitles_into_video_srt() -> None:
    plan = EditPlan0C(
        artifact_id="edit-plan-review-ep-w9",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=Producer(name="phase1-review-plane", version="1"),
        inputs=(),
        frame_rate=RATE,
        plan=EditPlanBody0C(
            plan_version="v1",
            edit_source=EditSourceRef0C(source_id="src-ep", total_frames=90),
            items=(
                EditPlanItem0C(
                    item_id="v1", kind="video", source_id="src-ep",
                    span=SourceFrameSpan(start_frame=0, end_frame=90, rate=RATE),
                    track_index=1,
                ),
                _subtitle_item("st1", "line-s1", 0, 30),
                _subtitle_item("st4", "line-s4", 90 - 30, 90),
            ),
        ),
    )

    ir = build_ir(plan, "ir-w9", ())
    subtitle_items = tuple(
        item for track in ir.tracks if track.track.kind == "subtitle" for item in track.items
    )
    srt = render_srt(expected_subtitle_cues(subtitle_items, ir.rate)).decode()

    assert [item.item_id for item in subtitle_items] == ["st1", "st4"]
    assert "line-s1" in srt
    assert "line-s4" in srt
    assert "line-s2" not in srt


def _episode_with_location_policy(tmp_path: Path) -> Path:
    episode_root = tmp_path / "ep-w9"
    store_dir = episode_root / "review" / "store"
    store_dir.mkdir(parents=True)
    seed = EditPlan0C(
        artifact_id="edit-plan-review-ep-seed",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=Producer(name="phase1-review-plane", version="1"),
        inputs=(),
        frame_rate=RATE,
        plan=EditPlanBody0C(
            plan_version="v1",
            edit_source=EditSourceRef0C(source_id="src-ep", total_frames=60),
            items=(
                EditPlanItem0C(
                    item_id="v1", kind="video", source_id="src-ep",
                    span=SourceFrameSpan(start_frame=0, end_frame=60, rate=RATE),
                    track_index=1,
                ),
            ),
        ),
    )
    initialize_store(seed, episode_root / "review" / "events.jsonl", store_dir)
    append_consultation(
        episode_root,
        ConsultationRecordV1(
            consultation_id="c1", created_at=now_stamp(), message="場所で変えたい"
        ),
    )
    append_proposal_set(
        episode_root,
        ConsultationProposalSetV1(
            consultation_id="c1",
            created_at=now_stamp(),
            proposals=(
                ConsultationProposalV1(
                    proposal_id="prop-1",
                    title="案1",
                    summary="要旨1",
                    details=ConsultationProposalDetails.model_validate(
                        {
                            "audience_message": "初めての人に",
                            "structure": "導入→本編→締め",
                            "duration_estimate": "約4分",
                            "candidate_scenes": ["opening"],
                            "subtitle_policy": "場所変更時のみ大テロップ",
                            "audio_policy": "BGM小さめ",
                            "tempo_policy": "前半重視",
                            "reference_mapping": "",
                            "unused_reasons": "",
                            "unconfirmed": [],
                            "presentation_condition": "location_change_only",
                        }
                    ),
                ),
            ),
        ),
    )
    append_judgment(
        episode_root,
        ConsultationJudgmentV1(
            judgment_id="j-w9",
            consultation_id="c1",
            proposal_id="prop-1",
            decision="adopt",
            scope=ConsultationScope(composition=True, appearance=True, audio=False),
            note=None,
            created_at=now_stamp(),
        ),
    )
    return episode_root


def test_stage_selection_records_applied_settings_in_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_root = _episode_with_location_policy(tmp_path)
    monkeypatch.setenv("EDITORIAL_DIRECTOR_API_KEY", "test-key")
    monkeypatch.setenv("EDITORIAL_DIRECTOR_NETWORK_ENABLED", "1")
    captured: dict[str, Any] = {}

    def fake_rerun(
        episode_root: Path, policy: Any, env: dict[str, str], runtime_path: Any = None
    ) -> object:
        captured["policy"] = policy
        namespace = SimpleNamespace(outcome=SimpleNamespace(request_hash="req-w9"))
        captured["rerun_marker"] = namespace
        return namespace

    def fake_derive(episode_root: Path, rerun: object, policy: Any = None) -> EditPlan0C:
        assert rerun is captured["rerun_marker"]
        assert policy is not None
        assert policy.presentation_condition == "location_change_only"
        head = load_head(
            episode_root / "review" / "events.jsonl", episode_root / "review" / "store"
        )
        return head.plan.model_copy(update={"artifact_id": "edit-plan-policy-v2"})

    monkeypatch.setattr(episode_runner_selection, "rerun_director_with_policy", fake_rerun)
    monkeypatch.setattr(episode_runner_selection, "derive_policy_plan", fake_derive)
    policy = latest_adopted_policy(episode_root)
    assert policy is not None
    assert policy.presentation_condition == "location_change_only"

    episode_runner_rebuild.stage_selection(episode_root, io.BytesIO(), policy=policy)

    outcomes = load_policy_outcomes(episode_root)
    assert len(outcomes) == 1
    assert outcomes[0].status == "connected"
    rows = {(row.policy_field, row.setting) for row in outcomes[0].applied_settings}
    assert (
        "appearance+subtitle_policy+presentation_condition=location_change_only",
        "review_subtitle_inclusion=boundary_only",
    ) in rows
    assert any("構成" in item for item in outcomes[0].unaddressed)
    journal_copy = load_policy_outcomes(episode_root)
    assert journal_copy[0].applied_settings == outcomes[0].applied_settings
    assert journal_copy[0].applied_settings[0].policy_field != ""
