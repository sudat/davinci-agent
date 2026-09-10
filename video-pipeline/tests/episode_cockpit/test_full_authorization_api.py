"""Full-authorization API pin + consultation revert gating (P1-2/P1-3).

Service level: the first 全編へ send binds and returns 200; a re-send
of the same authorization returns the same row with zero downstream
work (no render, no budget write, no spawn, no commit); a moved target
under the same operation id is a typed 409. Revert level: inside an
open consultation the revert stops typed BEFORE any head change; with
a bound authorization (or no consultation) it proceeds as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.consultation_store import (
    ConsultationProposalDetails,
    ConsultationProposalSetV1,
    ConsultationProposalV1,
    append_proposal_set,
    full_render_authorized,
    now_stamp,
)
from services.review_command.store import initialize_store, load_head
from tests.episode_cockpit.test_full_authorization_binding import (
    _DETAILS,
    _seed_consultation,
    _seed_policy_episode,
    _seed_second_consultation,
    authorize_bound,
    seed_viewed_sample,
)
from tests.review_command.support import manifest_plan


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
        "/episodes", json={"source_folder": str(source_folder), "brief_text": "auth e2e"}
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _seed_store(workspace: dict[str, Path], episode_id: str) -> Path:
    episode_dir = workspace["episodes_root"] / episode_id
    store_dir = episode_dir / "review" / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    initialize_store(
        manifest_plan("p0c-remove-clear"),
        episode_dir / "review" / "events.jsonl",
        store_dir,
    )
    return episode_dir


def _journal_snapshot(episode_dir: Path) -> dict[str, bytes | None]:
    names = (
        "judgments.jsonl",
        "budget.jsonl",
        "selection-budget.jsonl",
        "samples.jsonl",
        "policy-outcomes.jsonl",
    )
    snapshot: dict[str, bytes | None] = {}
    for name in names:
        path = episode_dir / "consultation" / name
        snapshot[name] = path.read_bytes() if path.is_file() else None
    rebuild = episode_dir / "rebuild-requests.jsonl"
    snapshot["rebuild-requests.jsonl"] = rebuild.read_bytes() if rebuild.is_file() else None
    return snapshot


def _authorize_payload(operation_id: str, sample_id: str | None = None) -> dict[str, object]:
    return {
        "consultation_id": "c1",
        "proposal_id": None,
        "decision": "full_authorized",
        "scope": {"composition": False, "appearance": False, "audio": False},
        "note": "全編へ",
        "operation_id": operation_id,
        "sample_id": sample_id,
    }


def test_judgment_endpoint_binds_and_resend_is_zero_downstream(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_store(workspace, episode_id)
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)

    first = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_authorize_payload("op-api-1", manifest.sample_id),
    )
    assert first.status_code == 200, first.text
    judgments = first.json()["judgments"]
    assert judgments[-1]["decision"] == "full_authorized"
    assert judgments[-1]["auth_sample_id"] is not None
    assert full_render_authorized(episode_dir) is True
    spawns_before = len(runner_spawn_calls)
    snapshot_before = _journal_snapshot(episode_dir)

    second = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_authorize_payload("op-api-1", manifest.sample_id),
    )
    assert second.status_code == 200, second.text
    assert second.json()["judgments"][-1]["judgment_id"] == judgments[-1]["judgment_id"]
    assert _journal_snapshot(episode_dir) == snapshot_before
    assert len(runner_spawn_calls) == spawns_before
    assert full_render_authorized(episode_dir) is True


def test_judgment_endpoint_moved_target_is_409(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_store(workspace, episode_id)
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)

    first = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_authorize_payload("op-api-2", manifest.sample_id),
    )
    assert first.status_code == 200, first.text
    changed = dict(_DETAILS)
    changed["tempo_policy"] = "後半テンポ重視"
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
                    details=ConsultationProposalDetails.model_validate(changed),
                ),
            ),
        ),
    )
    resend = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_authorize_payload("op-api-2", manifest.sample_id),
    )
    assert resend.status_code == 409, resend.text
    assert resend.json()["error"]["code"] == "consultation-judgment-conflict"
    assert full_render_authorized(episode_dir) is False


def test_judgment_endpoint_full_authorized_with_proposal_is_422(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_store(workspace, episode_id)
    _seed_policy_episode(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    payload = _authorize_payload("op-api-3", manifest.sample_id)
    payload["proposal_id"] = "prop-1"
    response = client.post(
        f"/episodes/{episode_id}/consultation/judgment", json=payload
    )
    assert response.status_code == 422, response.text
    assert (
        response.json()["error"]["code"]
        == "consultation-judgment-proposal-unexpected"
    )


def _apply_remove(client: TestClient, episode_id: str) -> None:
    text = "この区間を削除して"
    preview = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": text, "at_seconds": 6.0},
    )
    assert preview.status_code == 200, preview.text
    applied = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": text, "at_seconds": 6.0},
    )
    assert applied.status_code == 200, applied.text


def test_consultation_revert_stops_with_head_unchanged(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_store(workspace, episode_id)
    _apply_remove(client, episode_id)
    head_before = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    )
    assert head_before.version == 2
    _seed_consultation(episode_dir)
    events_before = (episode_dir / "review" / "events.jsonl").read_bytes()
    rebuild_log = episode_dir / "rebuild-requests.jsonl"
    rebuild_before = rebuild_log.read_bytes() if rebuild_log.is_file() else None
    spawns_before = len(runner_spawn_calls)

    response = client.post(f"/episodes/{episode_id}/review-chat/revert")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "consultation-revert-not-authorized"
    head_after = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    )
    assert head_after.version == head_before.version
    assert (episode_dir / "review" / "events.jsonl").read_bytes() == events_before
    if rebuild_before is None:
        assert not rebuild_log.is_file()
    else:
        assert rebuild_log.read_bytes() == rebuild_before
    assert len(runner_spawn_calls) == spawns_before


def test_authorized_revert_proceeds(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_store(workspace, episode_id)
    _apply_remove(client, episode_id)
    _seed_consultation(episode_dir)
    manifest = seed_viewed_sample(episode_dir)
    authorize_bound(episode_dir, sample_id=manifest.sample_id)
    assert full_render_authorized(episode_dir) is True

    response = client.post(f"/episodes/{episode_id}/review-chat/revert")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restored_from_version"] == "v1"
    assert body["new_version"] == "v3"
    head_after = load_head(
        episode_dir / "review" / "events.jsonl", episode_dir / "review" / "store"
    )
    assert head_after.version == 3


def test_judgment_endpoint_unspecified_sample_is_422(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_store(workspace, episode_id)
    _seed_policy_episode(episode_dir)
    seed_viewed_sample(episode_dir)
    response = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_authorize_payload("op-api-nosample"),
    )
    assert response.status_code == 422, response.text
    assert (
        response.json()["error"]["code"] == "consultation-authorization-no-sample"
    )
    assert full_render_authorized(episode_dir) is False


def test_judgment_endpoint_nonexistent_sample_is_422(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_store(workspace, episode_id)
    _seed_policy_episode(episode_dir)
    seed_viewed_sample(episode_dir)
    response = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_authorize_payload("op-api-ghost", "sample-0000000000000000"),
    )
    assert response.status_code == 422, response.text
    assert (
        response.json()["error"]["code"] == "consultation-authorization-no-sample"
    )
    assert full_render_authorized(episode_dir) is False


def test_judgment_endpoint_other_consultation_sample_is_422(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    episode_dir = _seed_store(workspace, episode_id)
    _seed_policy_episode(episode_dir)
    seed_viewed_sample(episode_dir)
    _seed_second_consultation(episode_dir)
    foreign = seed_viewed_sample(episode_dir, consultation_id="c2")
    response = client.post(
        f"/episodes/{episode_id}/consultation/judgment",
        json=_authorize_payload("op-api-foreign", foreign.sample_id),
    )
    assert response.status_code == 422, response.text
    assert (
        response.json()["error"]["code"] == "consultation-authorization-no-sample"
    )
    assert full_render_authorized(episode_dir) is False
