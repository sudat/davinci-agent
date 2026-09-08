"""工程2P: explicit rebuild linkage (予約→起動→成果) — complementary cases.

Covers the linkage write paths ``test_run_visibility.py`` does not: the
REVERT spawn record carrying ``run_id`` + ``target_version``, and the
plain-intent reservation surfacing as an unspawned tail entry. The
chain must be READABLE from the records without inference: a pre-spawn
reservation entry (sequence + stage hint + target version when
determinable), then a post-spawn record carrying the runner ``run_id``.
The spawner pre-generates the id and passes ``--run-id`` so the child's
log events and stage rows name the SAME run.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.status_view import load_rebuild_entries
from services.review_command.store import initialize_store
from tests.review_command.support import manifest_plan

REMOVE_TEXT = "この区間を削除して"
FEELINGS_AT = 6.0


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    return {"state_store": tmp_path / "state.db", "episodes_root": tmp_path / "jobs"}


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
    folder = tmp_path / "cam-a"
    folder.mkdir()
    (folder / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    return folder


def _create_episode(client: TestClient, source_folder: Path) -> str:
    response = client.post(
        "/episodes", json={"source_folder": str(source_folder), "brief_text": "travel vlog"}
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _seed_review_store(workspace: dict[str, Path], episode_id: str) -> None:
    episode_dir = workspace["episodes_root"] / episode_id
    initialize_store(
        manifest_plan("p0c-remove-clear"),
        episode_dir / "review" / "events.jsonl",
        episode_dir / "review" / "store",
    )


def _preview_and_apply_remove(client: TestClient, episode_id: str) -> dict[str, object]:
    preview = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": REMOVE_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert preview.status_code == 200
    applied = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": REMOVE_TEXT, "at_seconds": FEELINGS_AT},
    )
    assert applied.status_code == 200, applied.text
    return applied.json()["applied"]  # type: ignore[no-any-return]


def _argv_of(calls: list[dict[str, object]]) -> list[str]:
    return [str(argument) for argument in cast("list[str]", calls[-1]["argv"])]


def test_apply_spawn_links_reservation_to_run_id(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_review_store(workspace, episode_id)
    applied = _preview_and_apply_remove(client, episode_id)
    assert applied["result_plan_version"] == "v2"

    response = client.post(
        f"/episodes/{episode_id}/rebuild", json={"applied_command": applied["command_id"]}
    )
    assert response.status_code == 202
    assert response.json()["scheduled"] is True

    episode_dir = workspace["episodes_root"] / episode_id
    entries = load_rebuild_entries(episode_dir)
    assert [(entry.spawned, entry.run_id, entry.target_version) for entry in entries] == [
        (False, None, "v2"),  # the 予約 survives reload
        (True, entries[-1].run_id, "v2"),  # the 起動 names the SAME run
    ]
    argv = _argv_of(runner_spawn_calls)
    assert entries[-1].run_id is not None
    assert argv[argv.index("--run-id") + 1] == entries[-1].run_id


def test_revert_spawn_record_carries_run_id_and_target_version(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_review_store(workspace, episode_id)
    _preview_and_apply_remove(client, episode_id)

    response = client.post(f"/episodes/{episode_id}/review-chat/revert")
    assert response.status_code == 200, response.text
    assert response.json()["rebuild"]["scheduled"] is True

    episode_dir = workspace["episodes_root"] / episode_id
    (entry,) = load_rebuild_entries(episode_dir)
    assert entry.spawned is True
    assert entry.marker == "revert-v1"
    assert entry.target_version == "v3"  # the 成果 target is explicit
    assert entry.run_id is not None
    argv = _argv_of(runner_spawn_calls)
    assert argv[argv.index("--run-id") + 1] == entry.run_id


def test_plain_intent_reservation_surfaces_unspawned_tail(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)

    response = client.post(f"/episodes/{episode_id}/rebuild", json={"stage_hint": "preview"})
    assert response.status_code == 202
    assert response.json()["scheduled"] is False

    status = client.get(f"/episodes/{episode_id}").json()
    assert status["current_run"] is None  # nothing spawned — never an old run either
    latest = status["rebuild_requests"][-1]
    assert latest["stage_hint"] == "preview"
    assert latest["spawned"] is False
    assert latest["run_id"] is None  # 未起動 is readable from the payload
