"""Task 51: acceptance-flow backend wiring — references list/idempotency,
approval sessions (bundling over the real ledger), review-chat apply.

These routes exist so the Gate V43-4a UX flow can run Cockpit-only: the
operator lists persisted references, sees bundled approval sessions (at
most two normal blocking prompts, PRD 13.4), and turns a natural-language
correction into an applied command plus a lineage-scoped partial rebuild
— no CLI, no JSON hand-editing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from services.approvals.ingress import record_fixture_operation
from services.approvals.store import OperationRecordStore
from services.episode_cockpit.app import create_cockpit_app
from services.review_command.store import initialize_store
from tests.review_command.support import manifest_plan

if TYPE_CHECKING:
    from collections.abc import Iterator

    from services.approvals.models import ApprovalPurpose

# p0c-remove-clear: video v1[0,150) v2[150,300) v3[300,450) @30fps, total 750.
# at_seconds=1.0 -> frame 30 -> covering item v1.


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
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "受け入れフロー用"},
    )
    assert response.status_code == 200
    return str(response.json()["episode_id"])


def _seed_review_store(workspace: dict[str, Path], episode_id: str) -> None:
    base = workspace["episodes_root"] / episode_id
    initialize_store(
        manifest_plan("p0c-remove-clear"), base / "review" / "events.jsonl",
        base / "review" / "store",
    )


def _seed_pending_approvals(workspace: dict[str, Path], episode_id: str) -> None:
    store = OperationRecordStore(
        workspace["episodes_root"] / episode_id / "approvals" / "records.jsonl"
    )
    pending: tuple[tuple[ApprovalPurpose, str], ...] = (
        ("editorial", "1" * 64),
        ("presentation", "2" * 64),
        ("final", "3" * 64),
        ("publication", "4" * 64),
    )
    for purpose, target in pending:
        store.append(
            record_fixture_operation(
                purpose=purpose,
                target_bundle_hash=target,
                decision="reject",
                actor_id="pipeline-request",
            )
        )


# ---------------------------------------------------------------------------
# GET /references: persisted library list
# ---------------------------------------------------------------------------


def test_references_list_is_empty_before_any_registration(client: TestClient) -> None:
    response = client.get("/references")

    assert response.status_code == 200
    body = response.json()
    assert body == {"available": False, "references": []}


def test_references_list_reads_the_persisted_library(
    client: TestClient, tmp_path: Path
) -> None:
    ref_file = tmp_path / "style-ref.mp4"
    ref_file.write_bytes(b"unique-reference-bytes-a")
    registered = client.post("/references", json={"path": str(ref_file)})
    assert registered.status_code == 200

    listed = client.get("/references")

    assert listed.status_code == 200
    body = listed.json()
    assert body["available"] is True
    assert len(body["references"]) == 1
    entry = body["references"][0]
    assert entry["source_id"] == registered.json()["source_id"]
    assert entry["location"] == str(ref_file)
    assert entry["kind"] == "local_file"


# ---------------------------------------------------------------------------
# POST /references: idempotent registration (same content hash -> existing)
# ---------------------------------------------------------------------------


def test_reference_registration_is_idempotent_for_identical_content(
    client: TestClient, tmp_path: Path
) -> None:
    ref_file = tmp_path / "same-bytes.mp4"
    ref_file.write_bytes(b"identical-reference-bytes")

    first = client.post("/references", json={"path": str(ref_file)})
    assert first.status_code == 200
    first_body = first.json()

    second = client.post("/references", json={"path": str(ref_file)})

    assert second.status_code == 200
    second_body = second.json()
    assert second_body["source_id"] == first_body["source_id"]
    assert second_body["sha256"] == first_body["sha256"]
    assert second_body["library_version"] == first_body["library_version"]
    assert second_body["idempotent"] is True

    listed = client.get("/references").json()
    assert len(listed["references"]) == 1, "re-registration must not duplicate"


def test_reference_registration_still_appends_distinct_content(
    client: TestClient, tmp_path: Path
) -> None:
    first_file = tmp_path / "a.mp4"
    first_file.write_bytes(b"reference-content-a")
    second_file = tmp_path / "b.mp4"
    second_file.write_bytes(b"reference-content-b")

    first = client.post("/references", json={"path": str(first_file)}).json()
    second = client.post("/references", json={"path": str(second_file)}).json()

    assert second["idempotent"] is False
    assert second["library_version"] == first["library_version"] + 1
    assert len(client.get("/references").json()["references"]) == 2


# ---------------------------------------------------------------------------
# GET /episodes/{id}/approval-sessions: bundling over the real ledger
# ---------------------------------------------------------------------------


def test_approval_sessions_unknown_episode_is_structured_404(client: TestClient) -> None:
    response = client.get("/episodes/ep-doesnotexist/approval-sessions")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "episode-not-found"


def test_approval_sessions_empty_when_no_ledger(
    client: TestClient, source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)

    response = client.get(f"/episodes/{episode_id}/approval-sessions")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["sessions"] == []
    assert body["blocking_session_count"] == 0


def test_approval_sessions_bundle_pending_into_two_normal_sessions(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_pending_approvals(workspace, episode_id)

    body = client.get(f"/episodes/{episode_id}/approval-sessions").json()

    assert body["available"] is True
    assert body["blocking_session_count"] == 2
    keys = [session["session_key"] for session in body["sessions"]]
    assert keys == ["editorial-presentation", "final-publication"]
    purposes = {
        session["session_key"]: [item["purpose"] for item in session["items"]]
        for session in body["sessions"]
    }
    assert sorted(purposes["editorial-presentation"]) == ["editorial", "presentation"]
    assert sorted(purposes["final-publication"]) == ["final", "publication"]
    assert body["decided"] == []


def test_approval_sessions_clear_after_operator_approves_every_bundle(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_pending_approvals(workspace, episode_id)

    sessions = client.get(f"/episodes/{episode_id}/approval-sessions").json()
    for session in sessions["sessions"]:
        for item in session["items"]:
            executed = client.post(
                f"/episodes/{episode_id}/approvals/{item['record_id']}",
                json={"decision": "approve", "actor_id": "operator-accept"},
            )
            assert executed.status_code == 200

    after = client.get(f"/episodes/{episode_id}/approval-sessions").json()

    assert after["sessions"] == []
    assert after["blocking_session_count"] == 0
    decided_purposes = sorted(fact["purpose"] for fact in after["decided"])
    assert decided_purposes == ["editorial", "final", "presentation", "publication"]
    assert all(fact["decision"] == "approve" for fact in after["decided"])


# ---------------------------------------------------------------------------
# POST /episodes/{id}/review-chat/apply: NL correction -> command -> rebuild
# ---------------------------------------------------------------------------


def test_review_chat_apply_returns_applied_command_and_lineage_rebuild(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_review_store(workspace, episode_id)

    response = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": "この後2秒残して", "at_seconds": 1.0},
    )

    assert response.status_code == 200
    body = response.json()
    applied = body["applied"]
    assert applied["command_kind"] == "keep_longer"
    assert applied["affected_domain"] == "edit_plan"
    assert applied["result_plan_version"] == "v2"
    rebuild = body["rebuild"]
    assert rebuild["stages"] == [
        "plan", "compile", "preview", "resolve_build", "qc", "render"
    ]
    assert "selection" in rebuild["excluded_stages"]

    applied_log = (
        workspace["episodes_root"] / episode_id / "applied-commands.jsonl"
    ).read_text(encoding="utf-8")
    assert applied["command_id"] in applied_log

    rebuilt = client.post(
        f"/episodes/{episode_id}/rebuild",
        json={"applied_command": applied["command_id"]},
    )
    assert rebuilt.status_code == 202
    assert rebuilt.json()["scheduled"] is True
    assert rebuilt.json()["stages"] == list(rebuild["stages"])
    assert rebuilt.json()["runner_log"].endswith("runner.log")


def test_review_chat_apply_unconfirmed_draft_is_structured_422(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_review_store(workspace, episode_id)

    response = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": "ありがとうございます", "at_seconds": 1.0},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "draft-not-confirmed"


def test_review_chat_apply_unknown_episode_is_structured_404(client: TestClient) -> None:
    response = client.post(
        "/episodes/ep-doesnotexist/review-chat/apply",
        json={"text": "この後2秒残して", "at_seconds": 1.0},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "episode-not-found"
