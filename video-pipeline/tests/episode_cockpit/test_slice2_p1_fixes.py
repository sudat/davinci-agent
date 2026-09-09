"""Slice2 P1 fix wave: the 35 episode-level regressions (design-doc §回帰試験).

Store, API, runner, budget, and outcome proofs for idempotent adoptions,
pinned reservations with dual freshness checks, the cumulative selection
budget, connected/failed outcome honesty, the inherited runner-lock proof,
and orphan recovery with honest version reporting. Commit-level CAS proofs
live in tests/review_command/test_policy_commit_p1.py.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import threading
import time as time_module
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from services import foundation_io
from services.cli import episode_runner, episode_runner_rebuild, episode_runner_selection
from services.cli.episode_runner_rebuild import RebuildStageError
from services.cli.episode_runner_state import RunContext, record_stage
from services.cli.project import plan_sha256
from services.cli.review_common import store_ir, store_plan
from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
)
from services.contracts.primitives import Producer, RationalFrameRate, SourceFrameSpan
from services.editorial.prompt import render_adopted_policy_text
from services.episode_cockpit import api_consultation as consultation_api
from services.episode_cockpit import episode_files
from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.backend import CockpitWorkspace
from services.episode_cockpit.consultation_selection_budget import (
    attempt_for,
    reserve_director,
    reserve_preview,
    reserve_preview_full_rebuild_exempt,
    selection_budget_used,
    selection_budget_used_in_scope,
    settle_director,
    settle_preview,
    settle_preview_full_rebuild_exempt,
)
from services.episode_cockpit.consultation_store import (
    CONNECTED_POLICY_FIELDS,
    STRUCTURAL_REALIZED_CHECKS,
    ConsultationBudgetLimits,
    ConsultationJudgmentV1,
    ConsultationPolicyOutcomeV1,
    ConsultationProposalDetails,
    ConsultationProposalSetV1,
    ConsultationProposalV1,
    ConsultationRecordV1,
    ConsultationScope,
    append_consultation,
    append_effective_judgment_once,
    append_judgment,
    append_policy_outcome,
    append_proposal_set,
    budget_used,
    canonical_policy_sha256,
    consultation_view,
    consume_budget,
    derive_policy_rebuild,
    ensure_full_episode_budget_available,
    ensure_model_call_budget_available,
    ensure_preview_budget_available,
    latest_adopted_policy,
    load_budget_limits,
    load_consultations,
    load_judgments,
    load_policy_outcomes,
    model_attributed_calls,
    now_stamp,
    policy_for_judgment,
    policy_scope_list,
    policy_summary,
    remaining_wall_seconds,
)
from services.episode_cockpit.errors import CockpitUnprocessableError
from services.episode_cockpit.models import IntakeRecordV1, RebuildRequestEntry
from services.episode_cockpit.status_view import load_rebuild_entries
from services.foundation_io import canonical_model_bytes
from services.job_runner.cas import (
    ApprovalRef,
    TransitionPayload,
    apply_transition,
    current_job_state,
)
from services.job_runner.state_store import StateStore
from services.job_runner.transitions import (
    APPROVAL_PURPOSE_EDITORIAL,
    APPROVAL_PURPOSE_FINAL,
    MAIN_PATH,
)
from services.preview.tools import _capped
from services.review_command import commit as commit_module
from services.review_command import policy_commit as policy_commit_module
from services.review_command.policy_commit import (
    commit_policy,
    find_policy_event,
    reuse_committed_policy,
)
from services.review_command.store import (
    OperatorDecision0C,
    initialize_store,
    load_head,
)

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
_JUDGMENT_KWARGS: dict[str, Any] = {
    "consultation_id": "c1",
    "proposal_id": "prop-1",
    "decision": "adopt",
    "scope": ConsultationScope(composition=True, appearance=False, audio=False),
    "note": "構成だけ採用",
}


def _seed_plan(*, artifact_id: str = "edit-plan-review-ep-seed") -> EditPlan0C:
    def video(item_id: str, start: int, end: int) -> EditPlanItem0C:
        return EditPlanItem0C(
            item_id=item_id,
            kind="video",
            source_id="src-ep",
            span=SourceFrameSpan(start_frame=start, end_frame=end, rate=RATE),
            track_index=1,
        )

    return EditPlan0C(
        artifact_id=artifact_id,
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


def _seed_consultation(episode_dir: Path) -> None:
    append_consultation(
        episode_dir,
        ConsultationRecordV1(
            consultation_id="c1", created_at=now_stamp(), message="短くしたい"
        ),
    )
    append_proposal_set(
        episode_dir,
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


def _judge(
    episode_dir: Path, judgment_id: str, **overrides: Any
) -> ConsultationJudgmentV1:
    kwargs: dict[str, Any] = {
        "judgment_id": judgment_id,
        "created_at": now_stamp(),
        **_JUDGMENT_KWARGS,
        **overrides,
    }
    judgment = ConsultationJudgmentV1.model_validate(kwargs)
    append_judgment(episode_dir, judgment)
    return judgment


def _episode_with_policy(tmp_path: Path, name: str = "ep-policy") -> Path:
    episode_root = tmp_path / name
    store_dir = episode_root / "review" / "store"
    store_dir.mkdir(parents=True)
    initialize_store(_seed_plan(), episode_root / "review" / "events.jsonl", store_dir)
    _seed_consultation(episode_root)
    _judge(episode_root, "j1")
    return episode_root


def _reserve(episode_root: Path, judgment_id: str) -> RebuildRequestEntry:
    policy = policy_for_judgment(episode_root, judgment_id)
    head = load_head(
        episode_root / "review" / "events.jsonl", episode_root / "review" / "store"
    )
    log_path = episode_root / "rebuild-requests.jsonl"
    sequence = max(
        [entry.sequence for entry in load_rebuild_entries(episode_root)] + [0]
    ) + 1
    entry = RebuildRequestEntry(
        sequence=sequence,
        stage_hint="selection,plan,compile,preview",
        judgment_id=judgment_id,
        policy_scope=tuple(policy_scope_list(policy)),
        policy_sha256=canonical_policy_sha256(policy),
        base_plan_version=f"v{head.version}",
        base_plan_sha256=head.index.versions[str(head.version)].plan_sha256,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as stream:
        stream.write(canonical_model_bytes(entry) + b"\n")
    return entry


def _live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDITORIAL_DIRECTOR_API_KEY", "test-key")
    monkeypatch.setenv("EDITORIAL_DIRECTOR_NETWORK_ENABLED", "1")


def _fake_ok(  # noqa: PLR0913 (fake-seam wiring: monkeypatch + capture + behavior switches)
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, Any],
    *,
    on_call: Any = None,
    plan_factory: Any = None,
    fail_code: str | None = None,
    fail_detail: str = "boom",
) -> None:
    def fake_rerun(
        episode_root: Path, policy: Any, env: dict[str, str], runtime_path: Any = None
    ) -> object:
        captured["calls"] = captured.get("calls", 0) + 1
        if on_call is not None:
            on_call(episode_root, policy)
        if fail_code is not None:
            raise episode_runner_selection.SelectionRerunError(fail_code, fail_detail)
        return SimpleNamespace(
            outcome=SimpleNamespace(request_hash="req-hash-1"),
        )

    def fake_derive(episode_root: Path, rerun: object, policy: object = None) -> EditPlan0C:
        if plan_factory is not None:
            return plan_factory(episode_root)
        head = load_head(
            episode_root / "review" / "events.jsonl",
            episode_root / "review" / "store",
        )
        return head.plan.model_copy(update={"artifact_id": "edit-plan-policy-v2"})

    monkeypatch.setattr(
        episode_runner_selection, "rerun_director_with_policy", fake_rerun
    )
    monkeypatch.setattr(episode_runner_selection, "derive_policy_plan", fake_derive)


def _fast_forward_to(
    workspace: dict[str, Path], episode_id: str, target: str
) -> None:
    gated = {
        "EDITORIAL_APPROVED": APPROVAL_PURPOSE_EDITORIAL,
        "FINAL_APPROVED": APPROVAL_PURPOSE_FINAL,
    }
    with StateStore.open(workspace["state_store"]) as store:
        while True:
            current = current_job_state(store, episode_id)
            position = MAIN_PATH.index(current.status)
            if position >= MAIN_PATH.index(target):
                return
            nxt = MAIN_PATH[position + 1]
            payload = (
                TransitionPayload(
                    approval=ApprovalRef(
                        purpose=gated[nxt],
                        target_hash=current.adopted_artifact_hash or "",
                        artifact_ref=f"setup-ref-{gated[nxt]}",
                    )
                )
                if nxt in gated
                else None
            )
            apply_transition(
                store,
                episode_id,
                expected_status=current.status,
                expected_parent_hash=current.adopted_artifact_hash,
                new_status=nxt,
                new_artifact_hash="0" * 64,
                payload=payload,
            )


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    return {"state_store": tmp_path / "state.db", "episodes_root": tmp_path / "jobs"}


@pytest.fixture
def client(workspace: dict[str, Path]) -> Iterator[TestClient]:
    app = create_cockpit_app(
        state_store_path=workspace["state_store"], episodes_root=workspace["episodes_root"]
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def source_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "cam-a"
    folder.mkdir()
    (folder / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    return folder


def _create_episode(client: TestClient, source_folder: Path) -> str:
    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "P1修正テスト用"},
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _api_episode_with_seed(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> tuple[str, Path]:
    episode_id = _create_episode(client, source_folder)
    _fast_forward_to(workspace, episode_id, "PREVIEW_READY")
    episode_dir = workspace["episodes_root"] / episode_id
    _seed_consultation(episode_dir)
    return episode_id, episode_dir


def _judgment_payload(note: str = "構成だけ採用") -> dict[str, Any]:
    return {
        "consultation_id": "c1",
        "proposal_id": "prop-1",
        "decision": "adopt",
        "scope": {"composition": True, "appearance": False, "audio": False},
        "note": note,
    }


def _journal_counts(episode_dir: Path) -> dict[str, int]:
    def lines(path: Path) -> int:
        try:
            return len(path.read_bytes().splitlines())
        except OSError:
            return 0

    consultation = episode_dir / "consultation"
    review = episode_dir / "review"
    return {
        "judgments": lines(consultation / "judgments.jsonl"),
        "rebuilds": lines(episode_dir / "rebuild-requests.jsonl"),
        "outcomes": lines(consultation / "policy-outcomes.jsonl"),
        "budget": lines(consultation / "budget.jsonl"),
        "selection_budget": lines(consultation / "selection-budget.jsonl"),
        "events": lines(review / "events.jsonl"),
        "plans": len(list((review / "store").glob("plan-v*.json")))
        if (review / "store").is_dir()
        else 0,
    }


def test_append_effective_judgment_once_reuses_latest_exact_adoption(
    tmp_path: Path,
) -> None:
    episode_dir = tmp_path / "ep-j1"
    episode_dir.mkdir()
    _seed_consultation(episode_dir)

    first, appended_first = append_effective_judgment_once(
        episode_dir,
        consultation_id="c1",
        proposal_id="prop-1",
        decision="adopt",
        scope=ConsultationScope(composition=True, appearance=False, audio=False),
        note="構成だけ採用",
    )
    second, appended_second = append_effective_judgment_once(
        episode_dir,
        consultation_id="c1",
        proposal_id="prop-1",
        decision="adopt",
        scope=ConsultationScope(composition=True, appearance=False, audio=False),
        note="構成だけ採用",
    )

    assert appended_first is True
    assert appended_second is False
    assert second.judgment_id == first.judgment_id
    assert second == first
    assert len(load_judgments(episode_dir)) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"proposal_id": "prop-2"},
        {"decision": "revise"},
        {"scope": ConsultationScope(composition=False, appearance=True, audio=False)},
        {"scope": ConsultationScope(composition=True, appearance=True, audio=False)},
        {"scope": ConsultationScope(composition=True, appearance=False, audio=True)},
        {"note": "音も採用"},
        {"note": None},
        {"consultation_id": "c2"},
    ],
)
def test_append_effective_judgment_once_appends_when_any_fingerprint_field_differs(
    tmp_path: Path, override: dict[str, Any]
) -> None:
    episode_dir = tmp_path / "ep-j2"
    episode_dir.mkdir()
    _seed_consultation(episode_dir)
    base = {
        "consultation_id": "c1",
        "proposal_id": "prop-1",
        "decision": "adopt",
        "scope": ConsultationScope(composition=True, appearance=False, audio=False),
        "note": "構成だけ採用",
    }

    first, _ = append_effective_judgment_once(episode_dir, **base)
    second, appended = append_effective_judgment_once(
        episode_dir, **{**base, **override}
    )

    assert appended is True
    assert second.judgment_id != first.judgment_id
    assert len(load_judgments(episode_dir)) == 2


def test_duplicate_adoption_post_returns_existing_202_without_side_effects(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, episode_dir = _api_episode_with_seed(client, workspace, source_folder)

    first = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_judgment_payload(),
    )
    assert first.status_code == 202
    judgment_id = first.json()["judgments"][-1]["judgment_id"]
    before = _journal_counts(episode_dir)
    spawns_before = len(runner_spawn_calls)

    second = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_judgment_payload(),
    )

    assert second.status_code == 202
    assert second.json()["judgments"][-1]["judgment_id"] == judgment_id
    assert _journal_counts(episode_dir) == before
    assert len(runner_spawn_calls) == spawns_before


def test_duplicate_completed_adoption_post_returns_existing_200_without_v3(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, episode_dir = _api_episode_with_seed(client, workspace, source_folder)
    first = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_judgment_payload(),
    )
    assert first.status_code == 202
    judgment_id = first.json()["judgments"][-1]["judgment_id"]
    argv = cast("list[str]", runner_spawn_calls[-1]["argv"])
    run_id = argv[argv.index("--run-id") + 1]
    store_dir = episode_dir / "review" / "store"
    store_dir.mkdir(parents=True)
    log_path = episode_dir / "review" / "events.jsonl"
    initialize_store(_seed_plan(), log_path, store_dir)
    head = load_head(log_path, store_dir)
    plan_v2 = head.plan.model_copy(update={"artifact_id": "edit-plan-policy-v2"})
    committed = commit_policy(
        plan_v2,
        OperatorDecision0C(
            decision_id="dec-test", actor_intent="operator", note="adopt"
        ),
        log_path,
        store_dir,
        judgment_id=judgment_id,
        proposal_id="prop-1",
        policy_decision="adopt",
    )
    with StateStore.open(workspace["state_store"]) as store:
        record_stage(
            store,
            RunContext(
                workspace["state_store"], episode_id, run_id, "PREVIEW_READY",
                io.BytesIO(),
            ),
            "preview",
            "succeeded",
            adopted="0" * 64,
        )
    reservation = next(
        entry
        for entry in load_rebuild_entries(episode_dir)
        if entry.judgment_id == judgment_id and not entry.spawned
    )
    append_policy_outcome(
        episode_dir,
        ConsultationPolicyOutcomeV1(
            outcome_id="o-completed-1",
            consultation_id="c1",
            judgment_id=judgment_id,
            proposal_id="prop-1",
            plan_version="v2",
            status="connected",
            reasons=("adopted adopt policy re-run by the director",),
            note="採用した方針を編集長への入力に接続しました。",
            created_at=now_stamp(),
            reservation_sequence=reservation.sequence,
            run_id=run_id,
            commit_event_id=committed.event_id,
            director_connection="confirmed",
        ),
    )
    before = _journal_counts(episode_dir)
    spawns_before = len(runner_spawn_calls)

    second = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_judgment_payload(),
    )

    assert second.status_code == 200
    assert second.json()["judgments"][-1]["judgment_id"] == judgment_id
    assert load_head(log_path, store_dir).version == 2
    assert not (store_dir / "plan-v3.json").exists()
    assert _journal_counts(episode_dir) == before
    assert len(runner_spawn_calls) == spawns_before


def test_duplicate_adoption_still_validates_consultation_and_proposal_first(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id, _ = _api_episode_with_seed(client, workspace, source_folder)

    missing_consultation = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={**_judgment_payload(), "consultation_id": "c-unknown"},
    )
    missing_proposal = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={**_judgment_payload(), "proposal_id": "prop-unknown"},
    )

    assert missing_consultation.status_code == 404
    assert missing_consultation.json()["error"]["code"] == "consultation-not-found"
    assert missing_proposal.status_code == 422
    assert (
        missing_proposal.json()["error"]["code"]
        == "consultation-proposal-not-found"
    )


def test_spawn_failure_records_terminal_rebuild_state(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, episode_dir = _api_episode_with_seed(client, workspace, source_folder)
    store_dir = episode_dir / "review" / "store"
    store_dir.mkdir(parents=True)
    initialize_store(
        _seed_plan(), episode_dir / "review" / "events.jsonl", store_dir
    )
    judgment = _judge(episode_dir, "j1")

    def fail_spawn(*args: Any, **kwargs: Any) -> None:
        raise OSError("no room for another runner")

    monkeypatch.setattr(episode_files, "_spawn_runner", fail_spawn)
    cockpit = CockpitWorkspace(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )

    with pytest.raises(CockpitUnprocessableError) as exc_info:
        cockpit.record_consultation_rebuild(episode_id, judgment_id=judgment.judgment_id)

    assert "runner-spawn-failed" in str(exc_info.value)
    entries = load_rebuild_entries(episode_dir)
    terminal = entries[-1]
    assert terminal.judgment_id == judgment.judgment_id
    assert terminal.spawned is False
    assert terminal.run_id is None
    assert terminal.reserves_sequence == entries[-2].sequence
    assert terminal.failure_code == "runner-spawn-failed"
    assert terminal.detail == "再生成の起動に失敗しました。相談へ戻ってやり直せます。"
    policy = policy_for_judgment(episode_dir, judgment.judgment_id)
    assert derive_policy_rebuild(episode_dir, policy)["status"] == "failed"


def test_same_reservation_rerun_reuses_commit_without_director(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    policy = policy_for_judgment(episode_dir, "j1")
    reservation = _reserve(episode_dir, "j1")
    head = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    )
    commit_policy(
        head.plan.model_copy(update={"artifact_id": "edit-plan-policy-v2"}),
        OperatorDecision0C(
            decision_id="dec-test", actor_intent="operator", note="adopt"
        ),
        episode_dir / "review" / "events.jsonl",
        episode_dir / "review" / "store",
        judgment_id="j1",
        proposal_id="prop-1",
        policy_decision="adopt",
    )
    selection_journal = episode_dir / "consultation" / "selection-budget.jsonl"
    assert not selection_journal.exists()

    adopted = episode_runner_rebuild.stage_selection(
        episode_dir,
        io.BytesIO(),
        policy=policy,
        reservation_sequence=reservation.sequence,
        run_id="run-reuse-1",
        job_status="PREVIEW_READY",
    )

    assert captured.get("calls", 0) == 0
    head_after = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    )
    assert head_after.version == 2
    assert adopted == plan_sha256(head_after.plan)
    assert not selection_journal.exists()
    outcomes = load_policy_outcomes(episode_dir)
    assert outcomes[-1].status == "connected"
    assert outcomes[-1].plan_version == "v2"


def test_consultation_reservation_pins_scope_policy_and_base(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, episode_dir = _api_episode_with_seed(client, workspace, source_folder)
    store_dir = episode_dir / "review" / "store"
    store_dir.mkdir(parents=True)
    initialize_store(
        _seed_plan(), episode_dir / "review" / "events.jsonl", store_dir
    )
    judgment = _judge(episode_dir, "j1")
    policy = policy_for_judgment(episode_dir, "j1")
    cockpit = CockpitWorkspace(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )

    result = cockpit.record_consultation_rebuild(
        episode_id, judgment_id=judgment.judgment_id
    )

    reservation = next(
        entry
        for entry in load_rebuild_entries(episode_dir)
        if entry.judgment_id == judgment.judgment_id and not entry.spawned
    )
    assert reservation.policy_scope == tuple(policy_scope_list(policy))
    assert reservation.policy_sha256 == canonical_policy_sha256(policy)
    assert reservation.base_plan_version == "v1"
    head = load_head(episode_dir / "review" / "events.jsonl", store_dir)
    assert reservation.base_plan_sha256 == head.index.versions["1"].plan_sha256
    assert result["reservation_sequence"] == reservation.sequence
    spawn_argv = cast("list[str]", runner_spawn_calls[-1]["argv"])
    assert spawn_argv[spawn_argv.index("--reservation-sequence") + 1] == str(
        reservation.sequence
    )


def test_policy_for_judgment_loads_exact_nonlatest_judgment(
    tmp_path: Path,
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _judge(episode_dir, "j2", note="見た目も採用",
           scope=ConsultationScope(composition=True, appearance=True, audio=False))

    policy_a = policy_for_judgment(episode_dir, "j1")

    assert policy_a.judgment_id == "j1"
    assert policy_a.note == "構成だけ採用"
    assert policy_a.scope.composition is True
    assert policy_a.scope.appearance is False
    assert latest_adopted_policy(episode_dir) is not None
    assert latest_adopted_policy(episode_dir).judgment_id == "j2"  # type: ignore[union-attr]


def test_reservation_a_then_b_before_director_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    reservation_a = _reserve(episode_dir, "j1")
    _judge(episode_dir, "j2", note="見た目も採用")
    events_before = (episode_dir / "review" / "events.jsonl").read_bytes()

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation_a.sequence,
            run_id="run-a-1",
            job_status="PREVIEW_READY",
        )

    assert exc_info.value.code == "reserved-policy-changed"
    assert "予約した判断が変わりました" in exc_info.value.detail
    assert captured.get("calls", 0) == 0
    assert (episode_dir / "review" / "events.jsonl").read_bytes() == events_before
    assert load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    ).version == 1


def test_reservation_a_then_b_during_director_fails_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}

    def interleave(_root: Path, _policy: Any) -> None:
        _judge(episode_dir, "j2", note="見た目も採用")

    _fake_ok(monkeypatch, captured, on_call=interleave)
    reservation_a = _reserve(episode_dir, "j1")

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation_a.sequence,
            run_id="run-a-2",
            job_status="PREVIEW_READY",
        )

    assert exc_info.value.code == "reserved-policy-changed"
    assert captured.get("calls", 0) == 1
    assert load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    ).version == 1
    outcome = load_policy_outcomes(episode_dir)[-1]
    assert outcome.status == "failed"
    assert outcome.director_connection == "confirmed"
    assert outcome.failure_code == "reserved-policy-changed"


def test_reject_after_reservation_cancels_stale_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    reservation_a = _reserve(episode_dir, "j1")
    _judge(
        episode_dir, "j-reject", proposal_id=None, decision="reject",
        scope=ConsultationScope(), note=None,
    )
    events_before = (episode_dir / "review" / "events.jsonl").read_bytes()

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation_a.sequence,
            run_id="run-a-3",
            job_status="PREVIEW_READY",
        )

    assert exc_info.value.code == "reserved-policy-changed"
    assert captured.get("calls", 0) == 0
    assert (episode_dir / "review" / "events.jsonl").read_bytes() == events_before


def test_head_change_during_director_refuses_reserved_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}

    def advance_head(_root: Path, _policy: Any) -> None:
        head = load_head(
            episode_dir / "review" / "events.jsonl",
            episode_dir / "review" / "store",
        )
        commit_policy(
            head.plan.model_copy(update={"artifact_id": "edit-plan-external-v2"}),
            OperatorDecision0C(
                decision_id="dec-external", actor_intent="operator", note="external"
            ),
            episode_dir / "review" / "events.jsonl",
            episode_dir / "review" / "store",
            judgment_id="j-external",
            proposal_id=None,
            policy_decision="adopt",
        )

    _fake_ok(monkeypatch, captured, on_call=advance_head)
    reservation_a = _reserve(episode_dir, "j1")

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation_a.sequence,
            run_id="run-a-4",
            job_status="PREVIEW_READY",
        )

    assert exc_info.value.code == "policy-base-version-changed"
    assert "予約後に編集の版が変わりました" in exc_info.value.detail
    assert load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    ).version == 2
    assert not (episode_dir / "review" / "store" / "plan-v3.json").exists()


def test_legacy_consultation_reservation_without_pins_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    log_path = episode_dir / "rebuild-requests.jsonl"
    with log_path.open("ab") as stream:
        stream.write(
            canonical_model_bytes(
                RebuildRequestEntry(
                    sequence=1,
                    stage_hint="selection,plan,compile,preview",
                    judgment_id="j1",
                )
            )
            + b"\n"
        )

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=1,
            run_id="run-legacy-1",
            job_status="PREVIEW_READY",
        )

    assert exc_info.value.code == "consultation-reservation-unpinned"
    assert captured.get("calls", 0) == 0


def test_selection_budget_exhausted_stops_before_director(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    reservation = _reserve(episode_dir, "j1")
    for _ in range(6):
        consume_budget(
            episode_dir, "c1", llm_calls=1, intervals=0, wall_seconds=0.0
        )

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation.sequence,
            run_id="run-budget-1",
            job_status="PREVIEW_READY",
        )

    assert exc_info.value.code == "consultation-selection-budget-exhausted"
    assert captured.get("calls", 0) == 0
    assert load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    ).version == 1
    outcome = load_policy_outcomes(episode_dir)[-1]
    assert outcome.status == "failed"
    assert outcome.director_connection == "not_started"


def test_director_failure_settles_call_and_wall_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured, fail_code="director_failed")
    reservation = _reserve(episode_dir, "j1")

    with pytest.raises(RebuildStageError):
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation.sequence,
            run_id="run-budget-2",
            job_status="PREVIEW_READY",
        )

    used = selection_budget_used(episode_dir)
    assert used.llm_calls == 1
    assert used.wall_seconds > 0.0


def test_open_director_reservation_blocks_second_paid_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    policy = policy_for_judgment(episode_dir, "j1")
    reservation = _reserve(episode_dir, "j1")
    attempt = attempt_for(reservation.sequence, policy.consultation_id, "j1")
    reserve_director(episode_dir, attempt, 120.0)

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy,
            reservation_sequence=reservation.sequence,
            run_id="run-budget-3",
            job_status="PREVIEW_READY",
        )

    assert exc_info.value.code == "consultation-selection-attempt-uncertain"
    assert captured.get("calls", 0) == 0


def test_proposal_budget_view_includes_selection_usage(tmp_path: Path) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    consume_budget(episode_dir, "c1", llm_calls=1, intervals=1, wall_seconds=10.0)
    attempt = attempt_for(1, "c1", "j1")
    reserve_director(episode_dir, attempt, 120.0)
    settle_director(
        episode_dir, attempt, wall_elapsed=5.0, result="succeeded"
    )
    record = next(
        record
        for record in load_consultations(episode_dir)
        if record.consultation_id == "c1"
    )

    view = consultation_view(episode_dir, record, load_budget_limits())

    assert view["budget"]["llm_calls_used"] == 2
    assert view["budget"]["wall_seconds_used"] == pytest.approx(15.0)
    assert budget_used(episode_dir).llm_calls == 1


def test_preview_seconds_accumulate_across_rebuilds(tmp_path: Path) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    first = attempt_for(1, "c1", "j1")
    second = attempt_for(2, "c1", "j2")
    reserve_preview(episode_dir, first, 20.0)
    settle_preview(
        episode_dir, first, preview_seconds=20.0, wall_elapsed=1.0,
        result="succeeded",
    )
    reserve_preview(episode_dir, second, 5.0)
    settle_preview(
        episode_dir, second, preview_seconds=5.0, wall_elapsed=1.0,
        result="succeeded",
    )

    assert selection_budget_used(episode_dir).preview_seconds == pytest.approx(25.0)
    ensure_preview_budget_available(episode_dir, load_budget_limits(), 5.0)
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        ensure_preview_budget_available(episode_dir, load_budget_limits(), 6.0)
    assert exc_info.value.code == "consultation-preview-budget-exhausted"


def _huge_plan(episode_root: Path) -> EditPlan0C:
    head = load_head(
        episode_root / "review" / "events.jsonl", episode_root / "review" / "store"
    )
    item = head.plan.plan.items[0].model_copy(
        update={
            "span": SourceFrameSpan(start_frame=0, end_frame=3600, rate=RATE),
        }
    )
    return head.plan.model_copy(
        update={
            "artifact_id": "edit-plan-policy-huge",
            "plan": head.plan.plan.model_copy(update={"items": (item,)}),
        }
    )


def test_full_rebuild_over_remaining_sample_budget_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-10 ruling: a judgment-commissioned full re-render is not
    sample-budget-capped — it passes the preview gate and commits (the
    r9 blocker: a 282 s real-footage re-render used to fail-close here).
    """

    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured, plan_factory=_huge_plan)
    renders: list[str] = []
    monkeypatch.setattr(
        episode_runner_rebuild,
        "render_review_preview",
        lambda *args, **kwargs: renders.append("render") or "sha",
    )
    attempt = attempt_for(9, "c1", "j1")
    reserve_preview(episode_dir, attempt, 25.0)
    settle_preview(
        episode_dir, attempt, preview_seconds=25.0, wall_elapsed=1.0,
        result="succeeded",
    )
    reservation = _reserve(episode_dir, "j1")

    plan_sha = episode_runner_rebuild.stage_selection(
        episode_dir,
        io.BytesIO(),
        policy=policy_for_judgment(episode_dir, "j1"),
        reservation_sequence=reservation.sequence,
        run_id="run-preview-1",
        job_status="PREVIEW_READY",
    )

    assert renders == []
    assert load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    ).version == 2
    assert (episode_dir / "review" / "store" / "plan-v2.json").is_file()
    assert plan_sha == plan_sha256(
        store_plan(episode_dir / "review" / "store" / "plan-v2.json")
    )


def test_full_rebuild_preview_ledgers_exemption_not_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exempt re-render settles under "full_rebuild_exempt": visible
    in the journal and the wall fold, invisible to the sample totals and
    the sample gate (2026-09-10 ruling, honest ledger representation).
    """

    episode_dir = _episode_with_policy(tmp_path)
    head = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    )
    ir = store_ir(episode_dir / "review" / "store" / "ir-v1.json")
    precharged = attempt_for(1, "c1", "j1")
    reserve_preview(episode_dir, precharged, 29.0)
    settle_preview(
        episode_dir, precharged, preview_seconds=29.0, wall_elapsed=1.0,
        result="succeeded",
    )
    attempt = attempt_for(2, "c1", "j1")

    def fail_render(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("ffmpeg gone")

    monkeypatch.setattr(
        episode_runner_rebuild, "render_review_preview", fail_render
    )

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_preview(
            episode_dir, head, head.plan, ir, io.BytesIO(),
            run_id="run-preview-exempt", selection_attempt=attempt,
        )

    assert exc_info.value.code == "preview-failed"
    journal = episode_dir / "consultation" / "selection-budget.jsonl"
    lines = [
        json.loads(line) for line in journal.read_bytes().splitlines()
    ]
    exempt_lines = [line for line in lines if line["scope"] == "full_rebuild_exempt"]
    assert [line["phase"] for line in exempt_lines] == [
        "preview_reserved", "preview_settled",
    ]
    assert exempt_lines[-1]["preview_seconds_used"] == pytest.approx(2.0)
    assert exempt_lines[-1]["result"] == "failed"
    assert all(line["scope"] == "sample" for line in lines[:-2])
    used = selection_budget_used(episode_dir)
    assert used.preview_seconds == pytest.approx(29.0)
    exempt_used = selection_budget_used_in_scope(episode_dir, "full_rebuild_exempt")
    assert exempt_used.preview_seconds == pytest.approx(2.0)
    assert exempt_used.wall_seconds > 0.0
    limits = load_budget_limits()
    ensure_preview_budget_available(episode_dir, limits, 1.0)
    with pytest.raises(CockpitUnprocessableError) as gate_error:
        ensure_preview_budget_available(episode_dir, limits, 2.0)
    assert gate_error.value.code == "consultation-preview-budget-exhausted"


def test_wall_deadline_counts_full_rebuild_exempt_render_seconds(
    tmp_path: Path,
) -> None:
    """(d) The wall deadline stays fully in force for exempt re-renders:
    the deadline fold counts their wall seconds even though the sample
    gate ignores them.
    """

    episode_dir = _episode_with_policy(tmp_path)
    attempt = attempt_for(1, "c1", "j1")
    reserve_preview_full_rebuild_exempt(episode_dir, attempt, 60.0)
    settle_preview_full_rebuild_exempt(
        episode_dir, attempt, preview_seconds=60.0, wall_elapsed=601.0,
        result="succeeded",
    )

    assert episode_runner_rebuild.remaining_wall_seconds_with_exempt(
        episode_dir
    ) <= 0.0
    assert remaining_wall_seconds(episode_dir, load_budget_limits()) > 0.0
    carried = episode_runner_rebuild.ReentryState()
    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild._run_stage(
            "compile", episode_dir, carried, io.BytesIO(),
            run_id="run-deadline-exempt", reservation_sequence=1,
            job_status="PREVIEW_READY",
        )
    assert exc_info.value.code == "consultation-selection-deadline-exceeded"


def test_stage_preview_without_attempt_touches_no_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) The no-reservation (initial-chain style) preview path runs no
    budget gate and writes no selection-budget lines — unchanged.
    """

    episode_dir = _episode_with_policy(tmp_path)
    head = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    )
    ir = store_ir(episode_dir / "review" / "store" / "ir-v1.json")

    def fail_render(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("ffmpeg gone")

    monkeypatch.setattr(
        episode_runner_rebuild, "render_review_preview", fail_render
    )

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_preview(
            episode_dir, head, head.plan, ir, io.BytesIO(),
            run_id="run-preview-no-attempt",
        )

    assert exc_info.value.code == "preview-failed"
    assert not (
        episode_dir / "consultation" / "selection-budget.jsonl"
    ).exists()


def test_preview_failure_consumes_reserved_sample_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    head = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    )
    ir = store_ir(episode_dir / "review" / "store" / "ir-v1.json")
    attempt = attempt_for(3, "c1", "j1")

    def fail_render(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("ffmpeg gone")

    monkeypatch.setattr(
        episode_runner_rebuild, "render_review_preview", fail_render
    )

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_preview(
            episode_dir, head, head.plan, ir, io.BytesIO(),
            run_id="run-preview-2", selection_attempt=attempt,
        )

    assert exc_info.value.code == "preview-failed"
    used = selection_budget_used(episode_dir)
    assert used.preview_seconds == pytest.approx(2.0)
    journal = episode_dir / "consultation" / "selection-budget.jsonl"
    phases = list(journal.read_bytes().splitlines())
    assert len(phases) == 2


def test_wall_deadline_is_forwarded_to_director_and_preview_subprocesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    consume_budget(
        episode_dir, "c1", llm_calls=0, intervals=0, wall_seconds=480.0
    )
    reservation = _reserve(episode_dir, "j1")

    episode_runner_rebuild.stage_selection(
        episode_dir,
        io.BytesIO(),
        policy=policy_for_judgment(episode_dir, "j1"),
        reservation_sequence=reservation.sequence,
        run_id="run-deadline-1",
        job_status="PREVIEW_READY",
    )

    journal = episode_dir / "consultation" / "selection-budget.jsonl"
    reserved = next(
        line for line in journal.read_bytes().splitlines()
        if b"director_reserved" in line
    )
    assert b'"wall_seconds_reserved":120.0' in reserved
    assert _capped(600, 120.0) == 120.0
    assert _capped(120, 42.5) == 42.5
    assert _capped(600, None) == 600.0


def test_deadline_expiry_after_director_blocks_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    reservation = _reserve(episode_dir, "j1")
    ticks = iter([100.0, 110.0, 115.0, 200.0])
    monkeypatch.setattr(time_module, "monotonic", lambda: next(ticks))

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation.sequence,
            run_id="run-deadline-2",
            job_status="PREVIEW_READY",
            deadline_monotonic=150.0,
        )

    assert exc_info.value.code == "consultation-selection-deadline-exceeded"
    assert load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    ).version == 1
    used = selection_budget_used(episode_dir)
    assert used.llm_calls == 1


def test_connected_outcome_records_exact_policy_prompt_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    policy = policy_for_judgment(episode_dir, "j1")
    reservation = _reserve(episode_dir, "j1")

    episode_runner_rebuild.stage_selection(
        episode_dir,
        io.BytesIO(),
        policy=policy,
        reservation_sequence=reservation.sequence,
        run_id="run-conn-1",
        job_status="PREVIEW_READY",
    )

    outcome = load_policy_outcomes(episode_dir)[-1]
    assert outcome.status == "connected"
    assert outcome.director_connection == "confirmed"
    assert outcome.director_request_hash == "req-hash-1"
    expected_prompt_sha = hashlib.sha256(
        render_adopted_policy_text(policy_summary(policy)).encode()  # type: ignore[union-attr]
    ).hexdigest()
    assert outcome.policy_prompt_sha256 == expected_prompt_sha
    assert tuple(outcome.connected_fields) == tuple(CONNECTED_POLICY_FIELDS)
    assert outcome.reservation_sequence == reservation.sequence
    assert outcome.commit_event_id is not None
    assert outcome.run_id == "run-conn-1"
    assert outcome.plan_version == "v2"


def test_connected_outcome_lists_only_structural_realized_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    reservation = _reserve(episode_dir, "j1")

    episode_runner_rebuild.stage_selection(
        episode_dir,
        io.BytesIO(),
        policy=policy_for_judgment(episode_dir, "j1"),
        reservation_sequence=reservation.sequence,
        run_id="run-conn-2",
        job_status="PREVIEW_READY",
    )

    outcome = load_policy_outcomes(episode_dir)[-1]
    assert tuple(outcome.realized_checks) == tuple(STRUCTURAL_REALIZED_CHECKS)
    assert outcome.note is not None
    assert "実現した" not in outcome.note
    assert "意味" not in outcome.note


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (
            {"composition": True, "appearance": False, "audio": False},
            ("構成・候補場面・想定尺・参考対応・テンポが方針の意味どおりか",),
        ),
        (
            {"composition": False, "appearance": True, "audio": False},
            ("字幕と見た目が実映像で方針どおりか",),
        ),
        (
            {"composition": False, "appearance": False, "audio": True},
            (
                "BGM・音量・音付きテンポが方針どおりか",
                "audio_policy_detail: BGM・音量・音付きテンポの指定は見本の音設定にないため未対応",
            ),
        ),
    ],
)
def test_connected_outcome_lists_unaddressed_by_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flags: dict[str, bool],
    expected: tuple[str, ...],
) -> None:
    episode_root = tmp_path / f"ep-scope-{flags}"
    store_dir = episode_root / "review" / "store"
    store_dir.mkdir(parents=True)
    initialize_store(_seed_plan(), episode_root / "review" / "events.jsonl", store_dir)
    _seed_consultation(episode_root)
    _judge(episode_root, "j1", scope=ConsultationScope(**flags))
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    reservation = _reserve(episode_root, "j1")

    episode_runner_rebuild.stage_selection(
        episode_root,
        io.BytesIO(),
        policy=policy_for_judgment(episode_root, "j1"),
        reservation_sequence=reservation.sequence,
        run_id="run-conn-3",
        job_status="PREVIEW_READY",
    )

    outcome = load_policy_outcomes(episode_root)[-1]
    assert tuple(outcome.unaddressed) == expected
    assert tuple(outcome.unconfirmed) == ("尺の希望", "試し編集を本人が見て方針どおりか")


def test_failure_before_director_records_not_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    reservation = _reserve(episode_dir, "j1")
    for _ in range(6):
        consume_budget(
            episode_dir, "c1", llm_calls=1, intervals=0, wall_seconds=0.0
        )

    with pytest.raises(RebuildStageError):
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation.sequence,
            run_id="run-fail-1",
            job_status="PREVIEW_READY",
        )

    outcome = load_policy_outcomes(episode_dir)[-1]
    assert outcome.status == "failed"
    assert outcome.director_connection == "not_started"
    assert outcome.failure_code == "consultation-selection-budget-exhausted"
    assert tuple(outcome.realized_checks) == ()


def test_transport_failure_records_unknown_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured, fail_code="director_failed")
    reservation = _reserve(episode_dir, "j1")

    with pytest.raises(RebuildStageError):
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation.sequence,
            run_id="run-fail-2",
            job_status="PREVIEW_READY",
        )

    outcome = load_policy_outcomes(episode_dir)[-1]
    assert outcome.status == "failed"
    assert outcome.director_connection == "unknown"
    assert outcome.failure_code == "director_failed"


def test_new_writer_never_emits_honored_but_legacy_honored_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    reservation = _reserve(episode_dir, "j1")
    episode_runner_rebuild.stage_selection(
        episode_dir,
        io.BytesIO(),
        policy=policy_for_judgment(episode_dir, "j1"),
        reservation_sequence=reservation.sequence,
        run_id="run-honored-1",
        job_status="PREVIEW_READY",
    )
    legacy = ConsultationPolicyOutcomeV1(
        outcome_id="o-legacy-honored",
        consultation_id="c1",
        judgment_id="j1",
        proposal_id="prop-1",
        plan_version="v2",
        status="honored",
        reasons=("legacy writer",),
        note="legacy honored verdict",
        created_at=now_stamp(),
    )
    append_policy_outcome(episode_dir, legacy)

    outcomes = load_policy_outcomes(episode_dir)

    assert {outcome.status for outcome in outcomes} <= {"connected", "failed", "honored"}
    assert outcomes[0].status == "connected"
    assert outcomes[-1].status == "honored"
    assert outcomes[-1].plan_version == "v2"
    policy = policy_for_judgment(episode_dir, "j1")
    assert derive_policy_rebuild(episode_dir, policy)["target_version"] == "v2"


def _runner_episode(
    tmp_path: Path, name: str, workspace: dict[str, Path], target: str
) -> Path:
    episode_root = workspace["episodes_root"] / name
    episode_root.mkdir(parents=True)
    (episode_root / "intake.json").write_bytes(
        canonical_model_bytes(
            IntakeRecordV1(
                episode_id=name,
                source_folder=str(tmp_path),
                brief_text="lock test",
                created_at=now_stamp(),
            )
        )
    )
    with StateStore.open(workspace["state_store"]) as store:
        store.create_job(job_id=name, episode_id=name, current_stage="intake")
    _fast_forward_to(workspace, name, target)
    return episode_root


def _acquire_lock(episode_root: Path) -> int:
    fd = os.open(episode_root / "runner.lock", os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_reentry_without_inherited_runner_lock_refuses(
    tmp_path: Path, workspace: dict[str, Path]
) -> None:
    episode_root = _runner_episode(tmp_path, "ep-lock-1", workspace, "PREVIEW_READY")

    exit_code = episode_runner.run(
        episode_root=episode_root,
        stop="PREVIEW_READY",
        state_store_path=workspace["state_store"],
        from_stage="plan",
        applied_command="rcmd-x",
    )

    assert exit_code == episode_runner.EXIT_BLOCKED
    events = [
        line
        for line in (episode_root / "runner.log").read_text().splitlines()
        if '"blocked"' in line
    ]
    assert events
    assert json.loads(events[-1])["code"] == "runner-lock-not-held"
    assert not (episode_root / "rebuild-metrics.jsonl").exists()


def test_reentry_with_inherited_runner_lock_reaches_selection(
    tmp_path: Path, workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_root = _runner_episode(tmp_path, "ep-lock-2", workspace, "PREVIEW_READY")
    store_dir = episode_root / "review" / "store"
    store_dir.mkdir(parents=True)
    initialize_store(_seed_plan(), episode_root / "review" / "events.jsonl", store_dir)
    _seed_consultation(episode_root)
    _judge(episode_root, "j1")
    reservation = _reserve(episode_root, "j1")
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    monkeypatch.setattr(
        episode_runner_rebuild, "stage_preview", lambda *a, **k: "f" * 64
    )
    fd = _acquire_lock(episode_root)
    try:
        exit_code = episode_runner.run(
            episode_root=episode_root,
            stop="PREVIEW_READY",
            state_store_path=workspace["state_store"],
            from_stage="selection",
            applied_command="consultation-j1",
            reservation_sequence=reservation.sequence,
            run_id="run-lock-2",
            runner_lock_fd=fd,
        )
    finally:
        os.close(fd)

    assert exit_code == episode_runner.EXIT_SUCCESS
    assert load_head(
        episode_root / "review" / "events.jsonl", store_dir
    ).version == 2


def test_frozen_job_refuses_before_director_and_commit(
    tmp_path: Path, workspace: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path, name="ep-frozen-1")
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    reservation = _reserve(episode_dir, "j1")
    events_before = (episode_dir / "review" / "events.jsonl").read_bytes()

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation.sequence,
            run_id="run-frozen-1",
            job_status="FROZEN",
        )

    assert exc_info.value.code == "job-frozen"
    assert captured.get("calls", 0) == 0
    assert (episode_dir / "review" / "events.jsonl").read_bytes() == events_before

    episode_root = _runner_episode(tmp_path, "ep-frozen-2", workspace, "FROZEN")
    fd = _acquire_lock(episode_root)
    try:
        exit_code = episode_runner.run(
            episode_root=episode_root,
            stop="PREVIEW_READY",
            state_store_path=workspace["state_store"],
            from_stage="plan",
            applied_command="rcmd-frozen",
            runner_lock_fd=fd,
        )
    finally:
        os.close(fd)

    assert exit_code == episode_runner.EXIT_BLOCKED
    events = [
        json.loads(line)
        for line in (episode_root / "runner.log").read_text().splitlines()
        if '"blocked"' in line
    ]
    assert events[-1]["code"] == "job-frozen"


def test_persistent_policy_recovery_failure_reports_recorded_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path, name="ep-recovery-1")
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    reservation = _reserve(episode_dir, "j1")

    def fail_store_writes(path: Path, payload: bytes) -> None:
        if "store" in path.parts:
            raise OSError("persistent disk failure")
        foundation_io.atomic_write(path, payload)

    monkeypatch.setattr(policy_commit_module, "atomic_write", fail_store_writes)
    monkeypatch.setattr(commit_module, "atomic_write", fail_store_writes)

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation.sequence,
            run_id="run-recovery-1",
            job_status="PREVIEW_READY",
        )

    assert exc_info.value.code == "policy-commit-recovery-failed"
    outcome = load_policy_outcomes(episode_dir)[-1]
    assert outcome.status == "failed"
    assert outcome.plan_version == "v2"
    assert outcome.note != "版の確定前に失敗しました。方針は反映されていません。"
    assert "回復を確認できません" in (outcome.note or "")


def test_failed_rebuild_view_preserves_non_null_recorded_target_version(
    tmp_path: Path,
) -> None:
    episode_dir = _episode_with_policy(tmp_path, name="ep-failed-view-1")
    policy = policy_for_judgment(episode_dir, "j1")
    append_policy_outcome(
        episode_dir,
        ConsultationPolicyOutcomeV1(
            outcome_id="o-failed-v2",
            consultation_id="c1",
            judgment_id="j1",
            proposal_id="prop-1",
            plan_version="v2",
            status="failed",
            reasons=("policy-commit-recovery-failed",),
            note="編集の記録は v2 まで残っています。",
            created_at=now_stamp(),
            failure_code="policy-commit-recovery-failed",
            director_connection="confirmed",
        ),
    )

    view = derive_policy_rebuild(episode_dir, policy)

    assert view["status"] == "failed"
    assert view["target_version"] == "v2"


def test_a_then_b_interleaving_preserves_b_as_current_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path, name="ep-interleave-1")
    _live_env(monkeypatch)
    captured: dict[str, Any] = {}
    _fake_ok(monkeypatch, captured)
    reservation_a = _reserve(episode_dir, "j1")
    _judge(episode_dir, "j2", note="見た目も採用")
    reservation_b = _reserve(episode_dir, "j2")

    episode_runner_rebuild.stage_selection(
        episode_dir,
        io.BytesIO(),
        policy=policy_for_judgment(episode_dir, "j2"),
        reservation_sequence=reservation_b.sequence,
        run_id="run-interleave-b",
        job_status="PREVIEW_READY",
    )
    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild.stage_selection(
            episode_dir,
            io.BytesIO(),
            policy=policy_for_judgment(episode_dir, "j1"),
            reservation_sequence=reservation_a.sequence,
            run_id="run-interleave-a",
            job_status="PREVIEW_READY",
        )

    assert exc_info.value.code == "reserved-policy-changed"
    assert latest_adopted_policy(episode_dir) is not None
    assert latest_adopted_policy(episode_dir).judgment_id == "j2"  # type: ignore[union-attr]
    head = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    )
    assert head.version == 2
    assert find_policy_event(head.events, "j2") is not None
    assert find_policy_event(head.events, "j1") is None
    outcomes = load_policy_outcomes(episode_dir)
    assert outcomes[-2].status == "connected"
    assert outcomes[-1].status == "failed"


def _judgment_kwargs(note: str = "構成だけ採用") -> dict[str, Any]:
    return {
        "consultation_id": "c1",
        "proposal_id": "prop-1",
        "decision": "adopt",
        "scope": ConsultationScope(
            composition=True, appearance=False, audio=False
        ),
        "note": note,
    }


def test_w1_late_resend_dedupes_against_journal_not_latest(
    tmp_path: Path,
) -> None:
    episode_dir = tmp_path / "ep-w1-late"
    episode_dir.mkdir()
    _seed_consultation(episode_dir)

    adopted_a, _ = append_effective_judgment_once(
        episode_dir, **_judgment_kwargs("A案を採用")
    )
    adopted_b, _ = append_effective_judgment_once(
        episode_dir, **_judgment_kwargs("B案へ変更")
    )
    late_a, appended = append_effective_judgment_once(
        episode_dir, **_judgment_kwargs("A案を採用")
    )

    assert appended is False
    assert late_a.judgment_id == adopted_a.judgment_id
    assert len(load_judgments(episode_dir)) == 2
    policy = latest_adopted_policy(episode_dir)
    assert policy is not None
    assert policy.judgment_id == adopted_b.judgment_id


def test_w1_deliberate_readoption_with_new_operation_id_appends(
    tmp_path: Path,
) -> None:
    episode_dir = tmp_path / "ep-w1-deliberate"
    episode_dir.mkdir()
    _seed_consultation(episode_dir)

    first, _ = append_effective_judgment_once(
        episode_dir, **_judgment_kwargs(), operation_id="op-1"
    )
    deliberate, appended_new = append_effective_judgment_once(
        episode_dir, **_judgment_kwargs(), operation_id="op-2"
    )
    resend, appended_resend = append_effective_judgment_once(
        episode_dir, **_judgment_kwargs(), operation_id="op-1"
    )

    assert appended_new is True
    assert deliberate.judgment_id != first.judgment_id
    assert appended_resend is False
    assert resend.judgment_id == first.judgment_id
    assert len(load_judgments(episode_dir)) == 2


def test_w1_concurrent_posts_serialized_to_single_row(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep-w1-race"
    episode_dir.mkdir()
    _seed_consultation(episode_dir)
    barrier = threading.Barrier(8)
    results: list[tuple[str, bool]] = []

    def post_once() -> None:
        barrier.wait()
        judgment, appended = append_effective_judgment_once(
            episode_dir, **_judgment_kwargs()
        )
        results.append((judgment.judgment_id, appended))

    threads = [threading.Thread(target=post_once) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(load_judgments(episode_dir)) == 1
    assert {judgment_id for judgment_id, _ in results} == {
        load_judgments(episode_dir)[0].judgment_id
    }
    assert sum(1 for _, appended in results if appended) == 1


def test_w3_crash_window_resend_completes_unattempted_reservation(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, episode_dir = _api_episode_with_seed(client, workspace, source_folder)
    judgment, _ = append_effective_judgment_once(
        episode_dir, **_judgment_kwargs()
    )
    spawns_before = len(runner_spawn_calls)

    resend = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_judgment_payload(),
    )

    assert resend.status_code == 202
    assert resend.json()["judgments"][-1]["judgment_id"] == judgment.judgment_id
    assert len(runner_spawn_calls) == spawns_before + 1
    reserved = [
        entry
        for entry in load_rebuild_entries(episode_dir)
        if entry.judgment_id == judgment.judgment_id and not entry.spawned
    ]
    assert len(reserved) == 1


def test_w3_unknown_state_stops_honestly_without_reservation(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, episode_dir = _api_episode_with_seed(client, workspace, source_folder)
    older, _ = append_effective_judgment_once(
        episode_dir, **_judgment_kwargs("古いA案")
    )
    newer = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_judgment_payload("新しいB案"),
    )
    assert newer.status_code == 202
    rebuilds_before = _journal_counts(episode_dir)["rebuilds"]

    resend_old = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_judgment_payload("古いA案"),
    )

    assert resend_old.status_code == 200
    resend_ids = [
        judgment["judgment_id"] for judgment in resend_old.json()["judgments"]
    ]
    assert resend_ids == [older.judgment_id, newer.json()["judgments"][-1]["judgment_id"]]
    assert "reservation_recovery" not in resend_old.json()
    assert _journal_counts(episode_dir)["rebuilds"] == rebuilds_before
    assert not [
        entry
        for entry in load_rebuild_entries(episode_dir)
        if entry.judgment_id == older.judgment_id
    ]


def test_w3_running_a_saved_b_guides_resume_without_duplication(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, episode_dir = _api_episode_with_seed(client, workspace, source_folder)
    first = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_judgment_payload("A案を採用"),
    )
    assert first.status_code == 202
    saved_b = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_judgment_payload("B案へ変更"),
    )
    assert saved_b.status_code == 200
    judgment_b = saved_b.json()["judgments"][-1]["judgment_id"]
    rebuilds_before = _journal_counts(episode_dir)["rebuilds"]

    guided = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_judgment_payload("B案へ変更"),
    )

    assert guided.status_code == 200
    assert guided.json()["judgments"][-1]["judgment_id"] == judgment_b
    recovery = guided.json()["reservation_recovery"]
    assert recovery["status"] == "deferred_running"
    assert recovery["judgment_id"] == judgment_b
    assert _journal_counts(episode_dir)["rebuilds"] == rebuilds_before
    assert not [
        entry
        for entry in load_rebuild_entries(episode_dir)
        if entry.judgment_id == judgment_b
    ]

    argv = cast("list[str]", runner_spawn_calls[-1]["argv"])
    run_id = argv[argv.index("--run-id") + 1]
    with StateStore.open(workspace["state_store"]) as store:
        record_stage(
            store,
            RunContext(
                workspace["state_store"], episode_id, run_id, "PREVIEW_READY",
                io.BytesIO(),
            ),
            "preview",
            "succeeded",
            adopted="0" * 64,
        )

    resumed = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_judgment_payload("B案へ変更"),
    )

    assert resumed.status_code == 202
    reserved_b = [
        entry
        for entry in load_rebuild_entries(episode_dir)
        if entry.judgment_id == judgment_b and not entry.spawned
    ]
    assert len(reserved_b) == 1


def test_w4_proposal_transport_failure_consumes_cumulative_budget(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, episode_dir = _api_episode_with_seed(client, workspace, source_folder)
    before = budget_used(episode_dir)

    def boom(_message: str) -> dict:
        raise RuntimeError("line down")

    monkeypatch.setattr(consultation_api, "build_consultation_llm_call", lambda: boom)

    response = client.post(
        f"/episodes/{episode_id}/consultation/message",
        json={"message": "短くしたい"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consultation-llm-failed"
    used = budget_used(episode_dir)
    assert used.llm_calls == before.llm_calls + 1
    assert used.intervals == before.intervals + 1


def test_w4_unusable_envelope_still_settles_its_spend(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, episode_dir = _api_episode_with_seed(client, workspace, source_folder)
    before = budget_used(episode_dir)
    monkeypatch.setattr(
        consultation_api, "build_consultation_llm_call", lambda: (lambda _m: {})
    )

    response = client.post(
        f"/episodes/{episode_id}/consultation/message",
        json={"message": "短くしたい"},
    )

    assert response.status_code == 422
    assert budget_used(episode_dir).llm_calls == before.llm_calls + 1


def test_w4_input_byte_cap_fails_closed_before_any_model_contact(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, episode_dir = _api_episode_with_seed(client, workspace, source_folder)
    before = _journal_counts(episode_dir)["budget"]

    def tiny_limits() -> ConsultationBudgetLimits:
        limits = load_budget_limits()
        return limits.model_copy(update={"max_input_bytes_per_call": 1})

    monkeypatch.setattr(consultation_api, "load_budget_limits", tiny_limits)

    response = client.post(
        f"/episodes/{episode_id}/consultation/message",
        json={"message": "短くしたい"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consultation-input-over-cap"
    assert _journal_counts(episode_dir)["budget"] == before + 1


def test_w4_per_model_call_cap_binds_only_what_it_names(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep-w4-model"
    episode_dir.mkdir()
    limits = ConsultationBudgetLimits(per_model_llm_calls_limit={"model-x": 1})

    ensure_model_call_budget_available(episode_dir, limits, "model-x")
    ensure_model_call_budget_available(episode_dir, limits, "unlisted-model")
    ensure_model_call_budget_available(episode_dir, limits, None)
    consume_budget(
        episode_dir, "c1", llm_calls=1, intervals=1, wall_seconds=1.0,
        model_id="model-x",
    )

    assert model_attributed_calls(episode_dir, "model-x") == 1
    assert model_attributed_calls(episode_dir, "other-model") == 0
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        ensure_model_call_budget_available(episode_dir, limits, "model-x")
    assert exc_info.value.code == "consultation-model-budget-exhausted"
    ensure_model_call_budget_available(episode_dir, limits, "unlisted-model")


def test_w4_retry_breakdown_every_settle_line_counts(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep-w4-retry"
    episode_dir.mkdir()
    attempt = attempt_for(1, "c1", "j1")

    reserve_director(episode_dir, attempt, 120.0)
    settle_director(
        episode_dir, attempt, wall_elapsed=10.0,
        result="failed", failure_code="director-timeout",
    )
    settle_director(
        episode_dir, attempt, wall_elapsed=5.0,
        result="succeeded", retry_index=1,
    )

    used = selection_budget_used(episode_dir)
    assert used.llm_calls == 2
    assert used.wall_seconds == pytest.approx(15.0)


def test_w4_sample_full_episode_budget_boundary(tmp_path: Path) -> None:
    episode_dir = tmp_path / "ep-w4-scope"
    episode_dir.mkdir()
    sample = attempt_for(1, "c1", "j1")
    reserve_director(episode_dir, sample, 120.0)
    settle_director(episode_dir, sample, wall_elapsed=10.0, result="succeeded")
    full = attempt_for(2, "c1", "j1", scope="full_episode")
    reserve_director(episode_dir, full, 120.0)
    settle_director(episode_dir, full, wall_elapsed=20.0, result="succeeded")

    assert selection_budget_used(episode_dir).llm_calls == 1
    full_used = selection_budget_used_in_scope(episode_dir, "full_episode")
    assert full_used.llm_calls == 1
    assert full_used.wall_seconds == pytest.approx(20.0)
    tight = ConsultationBudgetLimits(full_episode_llm_calls_limit=1)
    with pytest.raises(CockpitUnprocessableError) as exc_info:
        ensure_full_episode_budget_available(episode_dir, tight)
    assert exc_info.value.code == "consultation-full-episode-budget-exhausted"
    ensure_full_episode_budget_available(episode_dir, ConsultationBudgetLimits())


def test_w4_remaining_deadline_applies_to_all_paths(tmp_path: Path) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    reservation = _reserve(episode_dir, "j1")
    attempt = attempt_for(reservation.sequence, "c1", "j1")
    settle_director(
        episode_dir, attempt, wall_elapsed=600.0,
        result="failed", failure_code="director-timeout",
    )

    with pytest.raises(RebuildStageError) as exc_info:
        episode_runner_rebuild._run_stage(
            "compile", episode_dir, episode_runner_rebuild.ReentryState(),
            io.BytesIO(), run_id="run-deadline-1",
            reservation_sequence=reservation.sequence,
            job_status="PREVIEW_READY",
        )
    assert exc_info.value.code == "consultation-selection-deadline-exceeded"


def test_w10_reused_commit_records_superseded_outcome_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_dir = _episode_with_policy(tmp_path)
    _live_env(monkeypatch)
    log_path = episode_dir / "review" / "events.jsonl"
    plan_dir = episode_dir / "review" / "store"
    head = load_head(log_path, plan_dir)
    decision = OperatorDecision0C(
        decision_id="dec-test", actor_intent="operator", note="adopt"
    )
    commit_policy(
        head.plan.model_copy(update={"artifact_id": "edit-plan-policy-a"}),
        decision, log_path, plan_dir,
        judgment_id="j1", proposal_id="prop-1", policy_decision="adopt",
    )
    _judge(episode_dir, "j2", note="第二案")
    commit_policy(
        head.plan.model_copy(update={"artifact_id": "edit-plan-policy-b"}),
        decision, log_path, plan_dir,
        judgment_id="j2", proposal_id="prop-1", policy_decision="adopt",
    )
    reservation = _reserve(episode_dir, "j1")
    reused = reuse_committed_policy(log_path, plan_dir, "j1")

    assert reused is not None
    assert reused.version == 2
    assert reused.superseded_by_head is True
    policy = policy_for_judgment(episode_dir, "j1")
    episode_runner_rebuild._connected_outcome(
        episode_dir, policy, plan_version="v2",
        reservation_sequence=reservation.sequence, run_id=None,
        commit_event_id=reused.event_id, director_request_hash=None,
        reused=True, superseded_by_head=reused.superseded_by_head,
    )

    rows = [
        outcome for outcome in load_policy_outcomes(episode_dir)
        if outcome.judgment_id == "j1"
    ]
    assert rows[-1].plan_version == "v2"
    assert rows[-1].superseded_by_head is True
