"""Consultation-triggered rebuild lineage ends at compile (P1-4d).

Hermetic: the runner spawn is stubbed by the episode_cockpit conftest
``runner_spawn_calls`` fixture (no processes, no ffmpeg, no LLM).
Given/When/Then per behavior.

An adoption rebuild must NOT reach the full preview render before the
operator's explicit 全編へ judgment — the recorded lineage is
selection→plan→compile with the sample flow as the terminal preview
surface. The spawned runner carries --stop PREVIEW_READY (the job's
terminal state stays PREVIEW_READY) plus the explicit --stop-stage
compile flag, so the runner truncates the chain at compile instead of
re-rendering; this test pins the reservation/spawn lineage this layer
controls.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
)
from services.contracts.primitives import Producer, RationalFrameRate, SourceFrameSpan
from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.backend import CockpitWorkspace
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
    now_stamp,
)
from services.episode_cockpit.review_chat import (
    AppliedCommand,
    record_applied_command,
)
from services.episode_cockpit.status_view import load_rebuild_entries
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
from services.review_command.store import initialize_store, load_head
from tests.episode_cockpit.test_full_authorization_binding import (
    _seed_plan_av,
    authorize_bound,
    seed_viewed_sample,
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


def test_consultation_rebuild_lineage_ends_at_compile(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    """Given a PREVIEW_READY episode with an adopted judgment, When the
    consultation rebuild is recorded, Then the lineage (response stages,
    stage hint, reservation row, spawn --from-stage) is
    selection→plan→compile with no preview stage — the adoption rebuild
    must not reach the full preview render before 全編へ."""

    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "P1-4d lineage"},
    )
    assert response.status_code == 200
    episode_id = str(response.json()["episode_id"])
    _fast_forward_to(workspace, episode_id, "PREVIEW_READY")
    episode_dir = workspace["episodes_root"] / episode_id
    store_dir = episode_dir / "review" / "store"
    store_dir.mkdir(parents=True)
    initialize_store(
        _seed_plan(), episode_dir / "review" / "events.jsonl", store_dir
    )
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
    judgment = ConsultationJudgmentV1.model_validate(
        {
            "judgment_id": "j1",
            "created_at": now_stamp(),
            "consultation_id": "c1",
            "proposal_id": "prop-1",
            "decision": "adopt",
            "scope": ConsultationScope(
                composition=True, appearance=False, audio=False
            ),
            "note": "構成だけ採用",
        }
    )
    append_judgment(episode_dir, judgment)
    head = load_head(episode_dir / "review" / "events.jsonl", store_dir)
    assert head.version == 1

    cockpit = CockpitWorkspace(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )
    result = cockpit.record_consultation_rebuild(
        episode_id, judgment_id=judgment.judgment_id
    )

    assert result["stages"] == ["selection", "plan", "compile"]
    assert "preview" not in cast("list[str]", result["stages"])
    assert result["stage_hint"] == "selection,plan,compile"
    reservation = next(
        entry
        for entry in load_rebuild_entries(episode_dir)
        if entry.judgment_id == judgment.judgment_id and not entry.spawned
    )
    assert reservation.stage_hint == "selection,plan,compile"
    argv = cast("list[str]", runner_spawn_calls[-1]["argv"])
    assert argv[argv.index("--from-stage") + 1] == "selection"
    assert argv[argv.index("--stop-stage") + 1] == "compile"
    assert argv[argv.index("--reservation-sequence") + 1] == str(
        reservation.sequence
    )
    assert result["reservation_sequence"] == reservation.sequence


def test_consultation_rebuild_reuse_keeps_compile_terminated_lineage(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    """Given a recorded consultation reservation, When it is recorded
    again, Then the reused response keeps the compile-terminated lineage
    and spawns nothing new."""

    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "P1-4d reuse"},
    )
    assert response.status_code == 200
    episode_id = str(response.json()["episode_id"])
    _fast_forward_to(workspace, episode_id, "PREVIEW_READY")
    episode_dir = workspace["episodes_root"] / episode_id
    store_dir = episode_dir / "review" / "store"
    store_dir.mkdir(parents=True)
    initialize_store(
        _seed_plan(), episode_dir / "review" / "events.jsonl", store_dir
    )
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
    judgment = ConsultationJudgmentV1.model_validate(
        {
            "judgment_id": "j1",
            "created_at": now_stamp(),
            "consultation_id": "c1",
            "proposal_id": "prop-1",
            "decision": "adopt",
            "scope": ConsultationScope(
                composition=True, appearance=False, audio=False
            ),
            "note": "構成だけ採用",
        }
    )
    append_judgment(episode_dir, judgment)
    cockpit = CockpitWorkspace(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )
    first = cockpit.record_consultation_rebuild(
        episode_id, judgment_id=judgment.judgment_id
    )
    spawns_before = len(runner_spawn_calls)

    second = cockpit.record_consultation_rebuild(
        episode_id, judgment_id=judgment.judgment_id
    )

    assert second["reused"] is True
    assert second["stages"] == ["selection", "plan", "compile"]
    assert first["reservation_sequence"] == second["reservation_sequence"]
    assert len(runner_spawn_calls) == spawns_before


def _episode_with_open_policy(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    brief: str,
    plan: EditPlan0C | None = None,
) -> tuple[str, Path]:
    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": brief},
    )
    assert response.status_code == 200
    episode_id = str(response.json()["episode_id"])
    _fast_forward_to(workspace, episode_id, "PREVIEW_READY")
    episode_dir = workspace["episodes_root"] / episode_id
    store_dir = episode_dir / "review" / "store"
    store_dir.mkdir(parents=True)
    initialize_store(
        plan or _seed_plan(), episode_dir / "review" / "events.jsonl", store_dir
    )
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
    append_judgment(
        episode_dir,
        ConsultationJudgmentV1.model_validate(
            {
                "judgment_id": "j1",
                "created_at": now_stamp(),
                "consultation_id": "c1",
                "proposal_id": "prop-1",
                "decision": "adopt",
                "scope": ConsultationScope(
                    composition=True, appearance=False, audio=False
                ),
                "note": "構成だけ採用",
            }
        ),
    )
    return episode_id, episode_dir


def test_consultation_nl_fix_rebuild_stops_before_preview(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    """Given an open consultation flow (adopted, no 全編へ), When an NL
    fix is recorded, Then the spawn carries the consultation-flow marker
    plus --stop-stage compile — the run terminates at the sample path
    and never reaches the full preview render."""

    episode_id, episode_dir = _episode_with_open_policy(
        client, workspace, source_folder, "P1-2 nl-stop"
    )
    record_applied_command(
        episode_dir,
        AppliedCommand(
            command_id="cmd-nl-1",
            command_kind="subtitle_shorter",
            affected_domain="presentation",
        ),
    )
    cockpit = CockpitWorkspace(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )
    result = cockpit.record_rebuild(
        episode_id, stage_hint=None, applied_command="cmd-nl-1"
    )

    assert result["scheduled"] is True
    assert result["stop_stage"] == "compile"
    argv = cast("list[str]", runner_spawn_calls[-1]["argv"])
    assert argv[argv.index("--from-stage") + 1] == "compile"
    assert argv[argv.index("--stop-stage") + 1] == "compile"
    assert argv[argv.index("--applied-command") + 1] == "cmd-nl-1"
    assert "--reservation-sequence" not in argv
    entries = load_rebuild_entries(episode_dir)
    markers = [entry.marker for entry in entries if entry.marker is not None]
    assert "consultation-flow:cmd-nl-1" in markers


def test_authorized_nl_fix_rebuild_runs_to_preview(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    """Given the explicit 全編へ judgment as latest, When an NL fix is
    recorded, Then the spawn carries the ordinary non-consultation
    marker with no stop — the full preview render stays connected."""

    episode_id, episode_dir = _episode_with_open_policy(
        client, workspace, source_folder, "P1-2 nl-authorized", plan=_seed_plan_av()
    )
    manifest = seed_viewed_sample(episode_dir)
    authorize_bound(episode_dir, sample_id=manifest.sample_id)
    record_applied_command(
        episode_dir,
        AppliedCommand(
            command_id="cmd-nl-2",
            command_kind="subtitle_shorter",
            affected_domain="presentation",
        ),
    )
    cockpit = CockpitWorkspace(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )
    result = cockpit.record_rebuild(
        episode_id, stage_hint=None, applied_command="cmd-nl-2"
    )

    assert result["scheduled"] is True
    assert "stop_stage" not in result
    argv = cast("list[str]", runner_spawn_calls[-1]["argv"])
    assert "--stop-stage" not in argv
    entries = load_rebuild_entries(episode_dir)
    markers = [entry.marker for entry in entries if entry.marker is not None]
    assert "review-command:cmd-nl-2" in markers
