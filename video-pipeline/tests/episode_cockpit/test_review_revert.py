"""UX redesign 工程1: revert endpoint (undo = NEW forward version).

The revert endpoint restores the previous plan version as a NEW version
through the 0C store (plan_restored event, no AppliedCommand), records a
rebuild request with its truthful spawn state, and detaches the runner
exactly like the apply flow. Spawn-failure honesty: the response reports
the committed restore accurately (200, ``scheduled: false``) and a
resend relaunches the SAME step instead of walking back again. Store
mechanics live in tests/review_command/test_commit_restore.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit import episode_ops
from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.review_chat import ReviewStoreLocation
from services.review_command.store import initialize_store, load_head
from tests.review_command.support import manifest_plan

if TYPE_CHECKING:
    from collections.abc import Iterator

FEELINGS_AT = 6.0
REMOVE_TEXT = "この区間を削除して"


def _flaky_popen(argv: list[str], **kwargs: object) -> object:
    if _FlakySubprocess.failing:
        raise OSError("spawn disabled for test")
    return object()


class _FlakySubprocess:
    """Spawn shim that fails on demand (spawn-failure regression tests)."""

    failing = False
    Popen = staticmethod(_flaky_popen)


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
        "/episodes", json={"source_folder": str(source_folder), "brief_text": "travel vlog"}
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _seed_store(workspace: dict[str, Path], episode_id: str) -> ReviewStoreLocation:
    store = ReviewStoreLocation(
        log_path=workspace["episodes_root"] / episode_id / "review" / "events.jsonl",
        plan_dir=workspace["episodes_root"] / episode_id / "review" / "store",
    )
    initialize_store(manifest_plan("p0c-remove-clear"), store.log_path, store.plan_dir)
    return store


def _preview_and_apply_remove(client: TestClient, episode_id: str) -> None:
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


def _revert_entries(workspace: dict[str, Path], episode_id: str) -> list[dict[str, object]]:
    log = workspace["episodes_root"] / episode_id / "rebuild-requests.jsonl"
    return [json.loads(line) for line in log.read_bytes().splitlines()]


def test_revert_restores_previous_plan_version_and_schedules_rebuild(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id = _create_episode(client, source_folder)
    store = _seed_store(workspace, episode_id)
    _preview_and_apply_remove(client, episode_id)
    v1_bytes = (store.plan_dir / "plan-v1.json").read_bytes()

    response = client.post(f"/episodes/{episode_id}/review-chat/revert")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restored_from_version"] == "v1"
    assert body["new_version"] == "v3"
    assert body["rebuild"]["scheduled"] is True
    assert body["rebuild"]["stages"][0] == "plan"
    assert body["rebuild"]["applied_command"] == "revert-v1"

    head = load_head(store.log_path, store.plan_dir)
    assert head.version == 3
    assert (store.plan_dir / "plan-v3.json").read_bytes() == v1_bytes
    assert head.events[-1].kind == "plan_restored"

    entries = _revert_entries(workspace, episode_id)
    assert [entry["spawned"] for entry in entries] == [True]
    assert entries[0]["marker"] == "revert-v1"
    assert len(runner_spawn_calls) == 2  # intake runner + revert rebuild
    argv = cast("list[str]", runner_spawn_calls[-1]["argv"])
    assert "--from-stage" in argv
    assert "plan" in argv
    assert "revert-v1" in argv


def test_second_revert_walks_one_more_step_back(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    store = _seed_store(workspace, episode_id)
    _preview_and_apply_remove(client, episode_id)
    v2_bytes = (store.plan_dir / "plan-v2.json").read_bytes()

    first = client.post(f"/episodes/{episode_id}/review-chat/revert")
    assert first.status_code == 200
    second = client.post(f"/episodes/{episode_id}/review-chat/revert")
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["restored_from_version"] == "v2"
    assert body["new_version"] == "v4"
    head = load_head(store.log_path, store.plan_dir)
    assert head.version == 4
    assert (store.plan_dir / "plan-v4.json").read_bytes() == v2_bytes


def test_revert_at_bootstrap_version_is_typed_409(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_store(workspace, episode_id)
    response = client.post(f"/episodes/{episode_id}/review-chat/revert")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "nothing-to-revert"


def test_revert_unknown_episode_is_structured_404(client: TestClient) -> None:
    response = client.post("/episodes/ep-missing/review-chat/revert")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "episode-not-found"


def test_revert_without_review_store_is_structured_422(
    client: TestClient, source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    response = client.post(f"/episodes/{episode_id}/review-chat/revert")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "review-store-not-initialized"


def test_revert_spawn_failure_reports_accurate_state_and_resend_relaunches(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id = _create_episode(client, source_folder)
    store = _seed_store(workspace, episode_id)
    _preview_and_apply_remove(client, episode_id)

    monkeypatch.setattr(episode_ops, "subprocess", _FlakySubprocess)
    _FlakySubprocess.failing = True
    failed = client.post(f"/episodes/{episode_id}/review-chat/revert")
    assert failed.status_code == 200, failed.text  # the restore DID commit
    failed_body = failed.json()
    assert failed_body["restored_from_version"] == "v1"
    assert failed_body["new_version"] == "v3"
    assert failed_body["rebuild"]["scheduled"] is False
    assert failed_body["rebuild"]["reason"] == "runner-start-failed"
    assert load_head(store.log_path, store.plan_dir).version == 3

    _FlakySubprocess.failing = False
    resent = client.post(f"/episodes/{episode_id}/review-chat/revert")
    assert resent.status_code == 200, resent.text
    resent_body = resent.json()
    assert resent_body["restored_from_version"] == "v1"  # SAME step, no walk-back
    assert resent_body["new_version"] == "v3"
    assert resent_body["rebuild"]["scheduled"] is True
    assert load_head(store.log_path, store.plan_dir).version == 3
    entries = _revert_entries(workspace, episode_id)
    assert [entry["spawned"] for entry in entries] == [False, True]
    assert {entry["marker"] for entry in entries} == {"revert-v1"}

    walked = client.post(f"/episodes/{episode_id}/review-chat/revert")
    assert walked.status_code == 200, walked.text
    assert walked.json()["restored_from_version"] == "v2"  # spawned: next step
