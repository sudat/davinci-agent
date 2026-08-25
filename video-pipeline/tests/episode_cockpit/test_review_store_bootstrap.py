"""Bootstrap resilience: apply repairs missing review/store from run/review-store."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.review_chat import (
    ReviewStoreLocation,
)
from services.review_command.store import initialize_store, load_head
from tests.review_command.support import manifest_plan

if TYPE_CHECKING:
    from collections.abc import Iterator


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


def _seed_run_store(episode_dir: Path) -> None:
    run_store = episode_dir / "run" / "review-store"
    run_store.mkdir(parents=True, exist_ok=True)
    # Use the same helper as other tests to get a valid genesis store
    tmp = episode_dir / "_tmp_init"
    tmp.mkdir(exist_ok=True)
    loc = ReviewStoreLocation(log_path=tmp / "events.jsonl", plan_dir=tmp / "store")
    initialize_store(manifest_plan("p0c-remove-clear"), loc.log_path, loc.plan_dir)
    # Copy into run/review-store flat layout (what the runner writes)
    for entry in sorted((tmp / "store").iterdir()):
        shutil.copyfile(entry, run_store / entry.name)
    for name in ("events.jsonl", "events.jsonl.seal"):
        # tmp layout: log lives next to tmp/events.jsonl, not in plan_dir
        # seal is tmp/events.jsonl.seal
        if name == "events.jsonl":
            shutil.copyfile(tmp / "events.jsonl", run_store / "events.jsonl")
        else:
            shutil.copyfile(tmp / "events.jsonl.seal", run_store / "events.jsonl.seal")
    shutil.rmtree(tmp)


def test_apply_bootstraps_from_run_store_when_cockpit_store_absent(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = workspace["episodes_root"] / episode_id
    _seed_run_store(episode_dir)
    # Ensure cockpit review/ does not exist yet
    assert not (episode_dir / "review" / "store" / "versions.json").exists()

    response = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": "この区間を削除して", "at_seconds": 6.0},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"]["command_kind"] == "remove_section"
    assert body["applied"]["result_plan_version"] == "v2"

    # Idempotency: existing v2 must not be re-bootstrapped
    # Re-applying same command should fail with target-not-in-plan
    second = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": "この区間を削除して", "at_seconds": 6.0},
    )
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "target-not-in-plan"
    # Cockpit store not overwritten by run store (still v2)
    head = load_head(episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store")
    assert head.version == 2


def test_apply_without_any_store_returns_typed_422(client: TestClient, source_folder: Path) -> None:
    episode_id = _create_episode(client, source_folder)
    response = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": "この区間を削除して", "at_seconds": 6.0},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "review-store-not-initialized"
    assert error["detail"]


def test_apply_with_existing_cockpit_store_does_not_clobber(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = workspace["episodes_root"] / episode_id
    # Seed cockpit store at v1, then apply once to reach v2
    cockpit_store = ReviewStoreLocation(
        log_path=episode_dir / "review" / "events.jsonl",
        plan_dir=episode_dir / "review" / "store",
    )
    initialize_store(
        manifest_plan("p0c-remove-clear"), cockpit_store.log_path, cockpit_store.plan_dir
    )
    first = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": "この区間を削除して", "at_seconds": 6.0},
    )
    assert first.status_code == 200
    assert first.json()["applied"]["result_plan_version"] == "v2"

    # Now plant a DIFFERENT run store (would be v1 if mirrored) — must NOT overwrite v2
    _seed_run_store(episode_dir)
    # Verify cockpit store still at v2 (not reset to v1)
    head = load_head(cockpit_store.log_path, cockpit_store.plan_dir)
    assert head.version == 2

    # Another distinct command should create v3, proving store was untouched
    second = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": "この後2秒残して", "at_seconds": 6.0},
    )
    # At 6s we cover v1 segment that was removed; after removal the plan differs
    # so we accept either success or target-not-in-plan — the point is versions.json was not reset
    assert second.status_code in (200, 422)
    head2 = load_head(cockpit_store.log_path, cockpit_store.plan_dir)
    # If second succeeded it would be v3, if it failed still v2 — but never v1
    assert head2.version in (2, 3)


def test_review_commit_error_maps_to_structured_422_not_500(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = workspace["episodes_root"] / episode_id
    # Seed a broken seal situation to trigger a non-store_not_initialized ReviewCommitError via HTTP
    # Easiest: create a store then corrupt its seal, then apply
    cockpit_store = ReviewStoreLocation(
        log_path=episode_dir / "review" / "events.jsonl",
        plan_dir=episode_dir / "review" / "store",
    )
    initialize_store(
        manifest_plan("p0c-remove-clear"), cockpit_store.log_path, cockpit_store.plan_dir
    )
    # Corrupt seal
    (episode_dir / "review" / "events.jsonl.seal").write_text("corrupt", encoding="utf-8")
    response = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": "この区間を削除して", "at_seconds": 6.0},
    )
    assert response.status_code == 422
    assert "error" in response.json()
    assert response.json()["error"]["code"] != "internal-error"
