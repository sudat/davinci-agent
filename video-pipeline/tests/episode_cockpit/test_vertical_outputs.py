"""工程5 vertical variant over the cockpit workspace (hermetic).

Given/When/Then per scope: output registration idempotence, separate
review chains per output, restore semantics per output, the
landscape-untouched guard (store file hashes before/after a vertical
build), approvals scoping, rebuild/judgment output binding, and the
default no-output_id behavior. No runner spawns (intent-only rebuilds),
no ffmpeg, no network.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from services.approvals.ingress import record_fixture_operation
from services.approvals.store import OperationRecordStore
from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.backend import CockpitWorkspace
from services.episode_cockpit.errors import CockpitNotFoundError
from services.episode_cockpit.models import RebuildRequestEntry
from services.review_command.commit import commit_command
from services.review_command.restore import commit_restore
from services.review_command.store import (
    OperatorDecision0C,
    initialize_store,
    load_head,
)
from tests.review_command.support import fixture_proposal, manifest_plan

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
    folder = tmp_path / "cam-a"
    folder.mkdir()
    (folder / "clip-001.mp4").write_bytes(b"fake-mp4-bytes")
    return folder


def _create_episode(client: TestClient, source_folder: Path) -> str:
    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "travel vlog, calm pacing"},
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _cockpit(workspace: dict[str, Path]) -> CockpitWorkspace:
    return CockpitWorkspace(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )


def _episode_dir(workspace: dict[str, Path], episode_id: str) -> Path:
    return workspace["episodes_root"] / episode_id


def _seed_landscape(episode_dir: Path) -> None:
    store_dir = episode_dir / "review" / "store"
    store_dir.mkdir(parents=True)
    initialize_store(
        manifest_plan("p0c-remove-clear"),
        episode_dir / "review" / "events.jsonl",
        store_dir,
    )


def _store_hashes(episode_dir: Path) -> dict[str, str]:
    digest: dict[str, str] = {}
    for path in (
        *(episode_dir / "review" / "store").iterdir(),
        episode_dir / "review" / "events.jsonl",
        episode_dir / "review" / "events.jsonl.seal",
    ):
        if path.is_file():
            digest[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def _decision(note: str = "vertical apply") -> OperatorDecision0C:
    return OperatorDecision0C(decision_id="dec-vert-001", actor_intent="operator", note=note)


def test_register_vertical_is_idempotent_over_http(
    client: TestClient, source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)

    first = client.post(f"/episodes/{episode_id}/outputs", json={"output_id": "vertical"})
    assert first.status_code == 200
    assert first.json()["registered"] == "vertical"
    assert first.json()["idempotent"] is False

    again = client.post(f"/episodes/{episode_id}/outputs", json={"output_id": "vertical"})
    assert again.status_code == 200
    assert again.json()["idempotent"] is True

    listed = client.get(f"/episodes/{episode_id}/outputs")
    assert [row["output_id"] for row in listed.json()["outputs"]] == [
        "landscape", "vertical",
    ]


def test_register_unknown_output_is_rejected(
    client: TestClient, source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)

    response = client.post(f"/episodes/{episode_id}/outputs", json={"output_id": "square"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown-output"


def test_vertical_commit_does_not_move_landscape_head(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _episode_dir(workspace, episode_id)
    _seed_landscape(episode_dir)
    cockpit = _cockpit(workspace)
    registered = cockpit.register_output(episode_id, "vertical")
    assert registered["idempotent"] is False

    vertical_log = episode_dir / "review" / "events-vertical.jsonl"
    vertical_store = episode_dir / "review" / "store-vertical"
    before = _store_hashes(episode_dir)
    landscape_head_before = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    ).version

    outcome = commit_command(
        fixture_proposal("p0c-span-clear"), _decision(), vertical_log, vertical_store
    )
    assert outcome.deferred is False

    landscape_head = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    )
    vertical_head = load_head(vertical_log, vertical_store)
    assert landscape_head.version == landscape_head_before == 1
    assert vertical_head.version == 2
    after = _store_hashes(episode_dir)
    for name, digest in before.items():
        assert after[name] == digest, f"landscape store file {name} changed by a vertical build"


def test_restore_semantics_are_per_output(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _episode_dir(workspace, episode_id)
    _seed_landscape(episode_dir)
    cockpit = _cockpit(workspace)
    cockpit.register_output(episode_id, "vertical")

    vertical_log = episode_dir / "review" / "events-vertical.jsonl"
    vertical_store = episode_dir / "review" / "store-vertical"
    commit_command(
        fixture_proposal("p0c-span-clear"), _decision(), vertical_log, vertical_store
    )
    restored = commit_restore(
        1,
        OperatorDecision0C(
            decision_id="dec-vert-revert", actor_intent="operator", note="revert"
        ),
        vertical_log,
        vertical_store,
    )
    assert restored.version == 3
    assert load_head(vertical_log, vertical_store).version == 3
    assert load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    ).version == 1


def test_approvals_are_scoped_per_output(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _episode_dir(workspace, episode_id)
    cockpit = _cockpit(workspace)
    cockpit.register_output(episode_id, "vertical")

    draft = record_fixture_operation(
        purpose="editorial",
        target_bundle_hash="a" * 64,
        decision="approve",
        actor_id="op-1",
    )
    OperationRecordStore(episode_dir / "approvals" / "records.jsonl").append(draft)

    landscape = cockpit.list_approvals(episode_id, "landscape")
    assert landscape["available"] is True
    vertical = cockpit.list_approvals(episode_id, "vertical")
    assert vertical["available"] is False
    assert vertical["approvals"] == []
    with pytest.raises(CockpitNotFoundError):
        cockpit.execute_approval(
            episode_id, "no-such-record", decision="approve", actor_id="op-1",
            output_id="vertical",
        )


def test_rebuild_requests_bind_output_id(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    cockpit = _cockpit(workspace)

    default = cockpit.record_rebuild(episode_id, stage_hint="plan")
    assert default["scheduled"] is False
    vertical = cockpit.record_rebuild(
        episode_id, stage_hint="plan", output_id="vertical"
    )
    assert vertical["scheduled"] is False

    log_path = _episode_dir(workspace, episode_id) / "rebuild-requests.jsonl"
    entries = [
        RebuildRequestEntry.model_validate_json(line)
        for line in log_path.read_bytes().splitlines()
    ]
    assert [entry.output_id for entry in entries] == ["landscape", "vertical"]


def test_vertical_preview_route_threads_output_dimension(
    client: TestClient, source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)

    missing = client.get(f"/episodes/{episode_id}/preview?output=vertical")
    assert missing.status_code == 404
    bad = client.get(f"/episodes/{episode_id}/preview?output=square")
    assert bad.status_code == 422


def test_default_requests_stay_landscape(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _episode_dir(workspace, episode_id)
    _seed_landscape(episode_dir)
    cockpit = _cockpit(workspace)

    flags = cockpit.review_flags(episode_id)
    assert flags["not_yet_generated"] is False
    sessions = cockpit.approval_sessions(episode_id)
    assert sessions["output_id"] == "landscape"
    assert cockpit.list_outputs(episode_id)["outputs"][0]["output_id"] == "landscape"
    binding: Any = cockpit.preview_binding(episode_id)
    assert binding is None or binding.output_id == "landscape"
