"""Operator data-authorization path for the editorial director (r3 measured gap).

Given/When/Then per case. The PRD §8 grant (transcript → editorial_direct
ONLY) is operator-supplied — never defaulted, never inferred — persisted
with the episode metadata, and the ONLY scope the API accepts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

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
    folder = tmp_path / "cam-grant"
    folder.mkdir()
    (folder / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    return folder


def _create(
    client: TestClient, source_folder: Path, payload_extra: dict[str, object] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_folder": str(source_folder),
        "brief_text": "grant lane episode",
    }
    if payload_extra is not None:
        payload.update(payload_extra)
    response = client.post("/episodes", json=payload)
    assert response.status_code == 200
    return response.json()


def _grant_file(workspace: dict[str, Path], episode_id: str) -> Path:
    return workspace["episodes_root"] / str(episode_id) / "editorial-grant.json"


def test_create_without_grant_declares_nothing(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    body = _create(client, source_folder)

    assert _grant_file(workspace, str(body["episode_id"])).exists() is False
    status = client.get(f"/episodes/{body['episode_id']}/editorial-grant")
    assert status.status_code == 200
    assert status.json()["granted"] is None


def test_create_with_grant_persists_operator_declaration(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    body = _create(
        client,
        source_folder,
        {"editorial_grant": {"granted": True, "note": "r3 lane"}},
    )

    record = json.loads(_grant_file(workspace, str(body["episode_id"])).read_bytes())
    assert record["granted"] is True
    assert record["data_class"] == "transcript"
    assert record["stage"] == "editorial_direct"
    assert record["note"] == "r3 lane"
    assert record["granted_at"] != ""


def test_grant_route_declares_revoke_and_regrant(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    body = _create(client, source_folder)
    episode_id = str(body["episode_id"])

    first = client.post(
        f"/episodes/{episode_id}/editorial-grant",
        json={"granted": True, "note": "first declaration"},
    )
    assert first.status_code == 200
    assert first.json()["granted"] is True

    second = client.post(
        f"/episodes/{episode_id}/editorial-grant",
        json={"granted": True, "note": "second declaration"},
    )
    assert second.status_code == 200
    assert second.json()["granted"] is True
    assert second.json()["granted_at"] >= first.json()["granted_at"]

    revoked = client.post(
        f"/episodes/{episode_id}/editorial-grant", json={"granted": False}
    )
    assert revoked.status_code == 200
    assert revoked.json()["granted"] is False

    stored = json.loads(_grant_file(workspace, episode_id).read_bytes())
    assert stored["granted"] is False

    fetched = client.get(f"/episodes/{episode_id}/editorial-grant")
    assert fetched.status_code == 200
    assert fetched.json()["granted"] is False


def test_grant_route_rejects_wider_scope(
    client: TestClient, source_folder: Path
) -> None:
    body = _create(client, source_folder)
    episode_id = str(body["episode_id"])

    for wider in (
        {"granted": True, "data_class": "audio"},
        {"granted": True, "stage": "moment_review"},
        {"granted": True, "data_class": "original_video", "stage": "moment_review"},
    ):
        response = client.post(
            f"/episodes/{episode_id}/editorial-grant", json=wider
        )
        assert response.status_code == 422


def test_grant_route_404s_unknown_episode(client: TestClient) -> None:
    response = client.post(
        "/episodes/ep-no-such-episode/editorial-grant", json={"granted": True}
    )
    assert response.status_code == 404

    fetched = client.get("/episodes/ep-no-such-episode/editorial-grant")
    assert fetched.status_code == 404
