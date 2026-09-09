"""Structured 本人確認 record (工程6 prep, additive).

Given/When/Then per case. Operator-supplied only — never defaulted,
never auto-created: absent file reads None everywhere (GET, status,
observer); null answers persist honestly as 未回答; non-bool answers
are 422, never coerced.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from services.cli.v44_observe import collect_observation_inputs
from services.episode_cockpit.app import create_cockpit_app

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    return {
        "state_store": tmp_path / "state.db",
        "episodes_root": tmp_path / "jobs",
    }


@pytest.fixture
def client(workspace: dict[str, Path]) -> Iterator[TestClient]:
    app = create_cockpit_app(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def source_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "cam-selfcheck"
    folder.mkdir()
    (folder / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    return folder


def _create(client: TestClient, source_folder: Path) -> str:
    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "self-check lane"},
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _log_path(workspace: dict[str, Path], episode_id: str) -> Path:
    return workspace["episodes_root"] / episode_id / "self-check.jsonl"


def test_never_auto_created_reads_null_everywhere(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create(client, source_folder)

    assert _log_path(workspace, episode_id).exists() is False
    assert client.get(f"/episodes/{episode_id}/self-check").json() == {
        "episode_id": episode_id,
        "self_check": None,
    }
    assert client.get(f"/episodes/{episode_id}").json()["self_check"] is None


def test_round_trip_true_false_and_note(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create(client, source_folder)

    response = client.post(
        f"/episodes/{episode_id}/self-check",
        json={
            "q_instruction_transmitted": True,
            "q_better_than_before": False,
            "q_want_to_publish": True,
            "note": "手触りは良い",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["episode_id"] == episode_id
    assert body["q_instruction_transmitted"] is True
    assert body["q_better_than_before"] is False
    assert body["q_want_to_publish"] is True
    assert body["note"] == "手触りは良い"
    assert body["answered_at"] != ""

    lines = _log_path(workspace, episode_id).read_bytes().splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored["schema_version"] == "v44-self-check-v1"
    assert stored["episode_id"] == episode_id

    assert client.get(f"/episodes/{episode_id}/self-check").json()["self_check"] == body
    assert client.get(f"/episodes/{episode_id}").json()["self_check"] == body


def test_null_answers_persist_honestly_as_unanswered(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create(client, source_folder)

    response = client.post(f"/episodes/{episode_id}/self-check", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["q_instruction_transmitted"] is None
    assert body["q_better_than_before"] is None
    assert body["q_want_to_publish"] is None
    assert body["note"] == ""

    response = client.post(
        f"/episodes/{episode_id}/self-check",
        json={"q_want_to_publish": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["q_instruction_transmitted"] is None
    assert body["q_want_to_publish"] is True
    assert len(_log_path(workspace, episode_id).read_bytes().splitlines()) == 2


def test_invalid_values_are_422_never_coerced(
    client: TestClient, source_folder: Path
) -> None:
    episode_id = _create(client, source_folder)

    for bad in (
        {"q_instruction_transmitted": 1},
        {"q_better_than_before": 0},
        {"q_want_to_publish": "yes"},
        {"q_instruction_transmitted": "true"},
        {"note": 123},
        {"unknown_question": True},
    ):
        response = client.post(f"/episodes/{episode_id}/self-check", json=bad)
        assert response.status_code == 422, bad

    assert client.get(f"/episodes/{episode_id}/self-check").json()["self_check"] is None


def test_observer_includes_latest_self_check(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create(client, source_folder)
    client.post(
        f"/episodes/{episode_id}/self-check",
        json={"q_instruction_transmitted": True},
    )
    client.post(
        f"/episodes/{episode_id}/self-check",
        json={"q_want_to_publish": False, "note": "最新が勝つ"},
    )

    inputs = collect_observation_inputs(
        episode_id,
        episodes_root=workspace["episodes_root"],
        state_store_path=workspace["state_store"],
    )
    assert inputs.self_check is not None
    assert inputs.self_check.q_instruction_transmitted is None
    assert inputs.self_check.q_want_to_publish is False
    assert inputs.self_check.note == "最新が勝つ"
