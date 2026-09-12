"""Same-folder retry: each create mints a fresh episode (codex fix G).

Two creates for the same source folder yield two distinct ids, both
episodes stay listed, and the first episode's workspace is intact.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app

if TYPE_CHECKING:
    from collections.abc import Iterator

import pytest


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
    folder = tmp_path / "cam-a"
    folder.mkdir()
    (folder / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    return folder


def test_same_folder_twice_mints_two_episodes(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    first = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "first take"},
    )
    second = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "second take"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    first_id = str(first.json()["episode_id"])
    second_id = str(second.json()["episode_id"])
    assert first_id != second_id
    assert first_id.startswith("ep-")
    assert second_id.startswith("ep-")

    listed = {row["episode_id"] for row in client.get("/episodes").json()["episodes"]}
    assert {first_id, second_id} <= listed

    first_dir = workspace["episodes_root"] / first_id
    assert (first_dir / "brief.json").is_file()
    assert (first_dir / "intake.json").is_file()
    first_status = client.get(f"/episodes/{first_id}")
    assert first_status.status_code == 200
