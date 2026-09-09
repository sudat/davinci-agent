"""Consultation slice 2: judgments schedule selection rebuilds (API level).

Adopt|revise with scope → 202 + the view carries the extracted policy and
the requested rebuild (reservation linked by judgment); reject (or an
empty scope, or a running rebuild) → 200 with the recorded judgment. The
rebuild consumes no consultation LLM budget.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit import api_consultation
from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.status_view import load_rebuild_entries
from services.job_runner.cas import apply_transition, current_job_state
from services.job_runner.state_store import StateStore
from services.job_runner.transitions import MAIN_PATH

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


def _envelope() -> dict[str, object]:
    return {
        "proposals": [
            {"title": "案1", "summary": "要旨1", "details": dict(_DETAILS)}
        ]
    }


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


@pytest.fixture
def llm_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def factory() -> Callable[[str], dict[str, object]]:
        def call(message: str) -> dict[str, object]:
            calls.append(message)
            return _envelope()

        return call

    monkeypatch.setattr(api_consultation, "build_consultation_llm_call", factory)
    return calls


def _create_episode(client: TestClient, source_folder: Path) -> str:
    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "相談フェーズ用"},
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _fast_forward_to_preview_ready(
    workspace: dict[str, Path], episode_id: str
) -> None:
    """Walk the job to PREVIEW_READY (P1 reservations schedule only there)."""

    target = MAIN_PATH.index("PREVIEW_READY")
    with StateStore.open(workspace["state_store"]) as store:
        while True:
            current = current_job_state(store, episode_id)
            position = MAIN_PATH.index(current.status)
            if position >= target:
                return
            apply_transition(
                store,
                episode_id,
                expected_status=current.status,
                expected_parent_hash=current.adopted_artifact_hash,
                new_status=MAIN_PATH[position + 1],
                new_artifact_hash="0" * 64,
                payload=None,
            )


def _consultation_id(client: TestClient, episode_id: str) -> str:
    response = client.post(
        f"/episodes/{episode_id}/consultation/message", json={"message": "短くしたい"}
    )
    assert response.status_code == 200
    return str(response.json()["consultation_id"])


def test_adopt_judgment_schedules_selection_rebuild_with_202(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    llm_calls: list[str],
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id = _create_episode(client, source_folder)
    _fast_forward_to_preview_ready(workspace, episode_id)
    consultation_id = _consultation_id(client, episode_id)

    response = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": consultation_id,
            "proposal_id": "prop-1",
            "decision": "adopt",
            "scope": {"composition": True, "appearance": False, "audio": False},
            "note": "構成だけ採用",
        },
    )

    assert response.status_code == 202
    view = response.json()
    assert set(view) >= {
        "consultation_id", "proposals", "judgments", "budget",
        "policy", "rebuild", "policy_outcomes",
    }
    adopted = view["policy"]["adopted"]
    assert adopted is not None
    assert adopted["judgment_id"] == view["judgments"][-1]["judgment_id"]
    assert adopted["proposal_id"] == "prop-1"
    assert adopted["structure"] == "導入→本編→締め"
    rebuild = view["rebuild"]
    assert rebuild["status"] == "requested"
    assert rebuild["target_version"] is None
    assert view["policy_outcomes"] == []
    assert len(runner_spawn_calls) == 2  # intake spawn + selection rebuild spawn
    argv = cast("list[str]", runner_spawn_calls[1]["argv"])
    assert argv[:7] == [
        sys.executable,
        "-m",
        "services.cli.episode_runner",
        "--episode-root",
        str(workspace["episodes_root"] / episode_id),
        "--stop",
        "PREVIEW_READY",
    ]
    assert argv[argv.index("--from-stage") + 1] == "selection"
    applied_command = argv[argv.index("--applied-command") + 1]
    assert applied_command == f"consultation-{adopted['judgment_id']}"
    episode_dir = workspace["episodes_root"] / episode_id
    entries = load_rebuild_entries(episode_dir)
    assert entries[-2].judgment_id == adopted["judgment_id"]
    assert entries[-1].judgment_id == adopted["judgment_id"]
    assert entries[-1].spawned is True


def test_reject_judgment_withdraws_and_returns_200(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    llm_calls: list[str],
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id = _create_episode(client, source_folder)
    _fast_forward_to_preview_ready(workspace, episode_id)
    consultation_id = _consultation_id(client, episode_id)
    adopt = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": consultation_id,
            "proposal_id": "prop-1",
            "decision": "adopt",
            "scope": {"composition": True, "appearance": False, "audio": False},
            "note": None,
        },
    )
    assert adopt.status_code == 202
    spawns_after_adopt = len(runner_spawn_calls)

    response = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": consultation_id,
            "proposal_id": None,
            "decision": "reject",
            "scope": {"composition": False, "appearance": False, "audio": False},
            "note": None,
        },
    )

    assert response.status_code == 200
    view = response.json()
    assert view["policy"]["adopted"] is None
    assert view["rebuild"]["status"] == "none"
    assert len(view["judgments"]) == 2  # append-only history stands
    assert len(runner_spawn_calls) == spawns_after_adopt  # no second spawn


def test_second_adopt_while_running_records_but_does_not_reschedule(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    llm_calls: list[str],
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id = _create_episode(client, source_folder)
    _fast_forward_to_preview_ready(workspace, episode_id)
    consultation_id = _consultation_id(client, episode_id)
    first = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": consultation_id,
            "proposal_id": "prop-1",
            "decision": "adopt",
            "scope": {"composition": True, "appearance": False, "audio": False},
            "note": None,
        },
    )
    assert first.status_code == 202
    spawns_after_first = len(runner_spawn_calls)

    second = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": consultation_id,
            "proposal_id": "prop-1",
            "decision": "revise",
            "scope": {"composition": False, "appearance": False, "audio": True},
            "note": "音も採用",
        },
    )

    assert second.status_code == 200
    view = second.json()
    assert len(view["judgments"]) == 2
    # The latest policy is the revise one, but no new rebuild is scheduled
    # while the first selection rebuild is still running.
    assert view["policy"]["adopted"]["decision"] == "revise"
    assert len(runner_spawn_calls) == spawns_after_first


def test_adopt_with_empty_scope_schedules_nothing(
    client: TestClient,
    source_folder: Path,
    llm_calls: list[str],
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id = _create_episode(client, source_folder)
    consultation_id = _consultation_id(client, episode_id)
    spawns_before = len(runner_spawn_calls)

    response = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": consultation_id,
            "proposal_id": "prop-1",
            "decision": "adopt",
            "scope": {"composition": False, "appearance": False, "audio": False},
            "note": None,
        },
    )

    assert response.status_code == 200
    view = response.json()
    assert view["policy"]["adopted"] is None
    assert view["rebuild"]["status"] == "none"
    assert len(runner_spawn_calls) == spawns_before


def test_judgment_consumes_no_consultation_budget(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    llm_calls: list[str],
) -> None:
    episode_id = _create_episode(client, source_folder)
    _fast_forward_to_preview_ready(workspace, episode_id)
    consultation_id = _consultation_id(client, episode_id)
    before = client.get(f"/episodes/{episode_id}/consultation").json()

    response = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json={
            "consultation_id": consultation_id,
            "proposal_id": "prop-1",
            "decision": "adopt",
            "scope": {"composition": True, "appearance": False, "audio": False},
            "note": None,
        },
    )

    assert response.status_code == 202
    after = client.get(f"/episodes/{episode_id}/consultation").json()
    assert (
        after["consultations"][0]["budget"]["llm_calls_used"]
        == before["consultations"][0]["budget"]["llm_calls_used"]
    )
    assert after["consultations"][0]["policy"]["adopted"] is not None
