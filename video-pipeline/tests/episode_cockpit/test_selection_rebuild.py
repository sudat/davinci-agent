"""Consultation slice 2: selection re-entry executes the policy loop.

Hermetic fake-seam pattern (mirrors ``test_rebuild_executor``): the review
store and the consultation journals are real (so the ``policy_applied``
commit, the fold, and the outcome journal are production code), while the
director re-run and the plan derivation are faked at the
``episode_runner_selection`` seam — the fake asserts the policy arrived in
its input. Failure paths record failed outcomes, commit no version, and
block the stage honestly.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.cli import episode_runner_rebuild, episode_runner_selection
from services.cli.episode_runner_rebuild import RebuildStageError, ReentryState
from services.cli.project import plan_sha256
from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
)
from services.contracts.primitives import Producer, RationalFrameRate, SourceFrameSpan
from services.episode_cockpit.consultation_store import (
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
)
from services.review_command.store import HeadState, initialize_store, load_head

RATE = RationalFrameRate(num=30, den=1)
_DETAILS = {
    "audience_message": "始めて見る人に分かる導入",
    "structure": "導入→本編→締め",
    "duration_estimate": "約4分",
    "candidate_scenes": ["opening", "demo"],
    "subtitle_policy": "短めの字幕",
    "audio_policy": "BGM小さめ",
    "tempo_policy": "前半テンポ重視",
    "reference_mapping": "参考1の構成を踏襲",
    "unused_reasons": "未使用素材はなし",
    "unconfirmed": ["尺の希望"],
}


def _seed_plan() -> EditPlan0C:
    def video(item_id: str, start: int, end: int) -> EditPlanItem0C:
        return EditPlanItem0C(
            item_id=item_id,
            kind="video",
            source_id="src-ep",
            span=SourceFrameSpan(start_frame=start, end_frame=end, rate=RATE),
            track_index=1,
        )

    return EditPlan0C(
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
            items=(video("v1", 0, 30), video("v2", 30, 60)),
        ),
    )


def _episode_with_policy(tmp_path: Path) -> Path:
    episode_root = tmp_path / "ep-policy"
    store_dir = episode_root / "review" / "store"
    store_dir.mkdir(parents=True)
    initialize_store(_seed_plan(), episode_root / "review" / "events.jsonl", store_dir)
    record = ConsultationRecordV1(
        consultation_id="c1", created_at=now_stamp(), message="短くしたい"
    )
    append_consultation(episode_root, record)
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
                    details=ConsultationProposalDetails.model_validate(_DETAILS),
                ),
            ),
        ),
    )
    append_judgment(
        episode_root,
        ConsultationJudgmentV1(
            judgment_id="j-policy-1",
            consultation_id="c1",
            proposal_id="prop-1",
            decision="adopt",
            scope=ConsultationScope(
                composition=True, appearance=False, audio=False
            ),
            note="構成だけ採用",
            created_at=now_stamp(),
        ),
    )
    return episode_root


def _live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITORIAL_DIRECTOR_API_KEY", "test-key")
    monkeypatch.setenv("EDITORIAL_DIRECTOR_NETWORK_ENABLED", "1")


def _fake_seams(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any], *, fail_derive: bool = False
) -> None:
    def fake_rerun(
        episode_root: Path, policy: Any, env: dict[str, str], runtime_path: Any = None
    ) -> object:
        captured["policy"] = policy
        captured["env_keys"] = sorted(env)
        return SimpleNamespace(
            outcome=SimpleNamespace(request_hash="test-request-hash"),
        )

    def fake_derive(episode_root: Path, rerun: object, policy: Any = None) -> EditPlan0C:
        assert rerun is captured["rerun_marker"]
        if fail_derive:
            raise episode_runner_selection.PolicyDerivationError(
                "planner-infeasible", "no feasible plan under the adopted policy"
            )
        head = load_head(
            episode_root / "review" / "events.jsonl",
            episode_root / "review" / "store",
        )
        return head.plan.model_copy(update={"artifact_id": "edit-plan-policy-v2"})

    def recording_rerun(
        episode_root: Path, policy: Any, env: dict[str, str], runtime_path: Any = None
    ) -> object:
        marker = fake_rerun(episode_root, policy, env)
        captured["rerun_marker"] = marker
        return marker

    monkeypatch.setattr(
        episode_runner_selection, "rerun_director_with_policy", recording_rerun
    )
    monkeypatch.setattr(episode_runner_selection, "derive_policy_plan", fake_derive)


def test_selection_honored_commits_new_version_and_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_root = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_seams(monkeypatch, captured)
    policy = latest_adopted_policy(episode_root)
    assert policy is not None

    adopted = episode_runner_rebuild.stage_selection(
        episode_root, io.BytesIO(), policy=policy
    )

    assert captured["policy"].judgment_id == "j-policy-1"
    head = load_head(
        episode_root / "review" / "events.jsonl", episode_root / "review" / "store"
    )
    assert head.version == 2
    assert (episode_root / "review" / "store" / "plan-v2.json").is_file()
    assert adopted == plan_sha256(head.plan)
    outcomes = load_policy_outcomes(episode_root)
    assert len(outcomes) == 1
    assert outcomes[0].status == "connected"
    assert outcomes[0].plan_version == "v2"
    assert outcomes[0].judgment_id == "j-policy-1"


def test_selection_failure_records_failed_outcome_and_commits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_root = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_seams(monkeypatch, captured, fail_derive=True)
    policy = latest_adopted_policy(episode_root)
    assert policy is not None

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_root, io.BytesIO(), policy=policy
        )

    assert exc_info.value.code == "planner-infeasible"
    assert not (episode_root / "review" / "store" / "plan-v2.json").exists()
    head = load_head(
        episode_root / "review" / "events.jsonl", episode_root / "review" / "store"
    )
    assert head.version == 1
    outcomes = load_policy_outcomes(episode_root)
    assert len(outcomes) == 1
    assert outcomes[0].status == "failed"
    assert outcomes[0].plan_version is None
    assert "planner-infeasible" in outcomes[0].reasons[0]


def test_selection_baseline_mode_fails_honestly_without_interpreting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_root = _episode_with_policy(tmp_path)
    monkeypatch.delenv("EDITORIAL_DIRECTOR_API_KEY", raising=False)
    monkeypatch.delenv("EDITORIAL_DIRECTOR_NETWORK_ENABLED", raising=False)
    # Pin the diagnostic runtime explicitly: the director resolves explicit >
    # env > repo default (shipped production+codex-exec), so the baseline
    # refusal needs a declared heuristic runtime, not just missing keys.
    heuristic = tmp_path / "editorial-runtime-heuristic.json"
    heuristic.write_text('{"mode": "heuristic_diagnostic"}', encoding="utf-8")
    monkeypatch.setenv("EDITORIAL_RUNTIME_CONFIG", str(heuristic))
    policy = latest_adopted_policy(episode_root)
    assert policy is not None

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_root, io.BytesIO(), policy=policy
        )

    assert exc_info.value.code == "policy-not-interpretable"
    assert exc_info.value.detail == (
        "編集長が決定論化モードのため方針を解釈できませんでした"
    )
    assert not (episode_root / "review" / "store" / "plan-v2.json").exists()
    outcomes = load_policy_outcomes(episode_root)
    assert len(outcomes) == 1
    assert outcomes[0].status == "failed"


def test_run_stage_selection_routes_policy_and_refreshes_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_root = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_seams(monkeypatch, captured)
    carried = ReentryState()

    adopted = episode_runner_rebuild._run_stage(
        "selection", episode_root, carried, io.BytesIO(), run_id="run-sel-01"
    )

    assert captured["policy"].judgment_id == "j-policy-1"
    assert isinstance(carried.head, HeadState)
    assert carried.head.version == 2
    assert carried.plan is None
    assert adopted == plan_sha256(carried.head.plan)


def test_run_stage_selection_without_policy_is_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_root = tmp_path / "ep-bare"
    store_dir = episode_root / "review" / "store"
    store_dir.mkdir(parents=True)
    initialize_store(_seed_plan(), episode_root / "review" / "events.jsonl", store_dir)

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild._run_stage(
            "selection", episode_root, ReentryState(), io.BytesIO(), run_id="run-x"
        )

    assert exc_info.value.code == "no-adopted-policy"
