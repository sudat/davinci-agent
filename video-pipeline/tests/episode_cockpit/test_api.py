"""Task 44: the loopback-only Cockpit FastAPI surface over EXISTING state.

Given/When/Then per route group. The cockpit NEVER owns a second state
machine: episodes live in the job-runner StateStore (SQLite) plus
episode workspace files; every write goes through the existing service
APIs (StateStore.create_job, reference_learning ingest, approvals store
append). Loopback discipline is enforced at factory time.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from services.approvals.ingress import record_fixture_operation
from services.approvals.store import OperationRecordStore
from services.episode_cockpit.app import (
    LOOPBACK_HOST,
    CockpitBindError,
    create_cockpit_app,
    run,
)
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_models import StageRunRow
from services.job_runner.state_store import StateStore
from services.preview.render import PREVIEW_NAME
from services.reference_learning.models import ReferenceLibraryV1

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


def _create_episode(client: TestClient, source_folder: Path) -> dict[str, object]:
    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "travel vlog, calm pacing"},
    )
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# (a) create episode -> job row exists in the existing StateStore
# ---------------------------------------------------------------------------


def test_create_episode_persists_job_row_in_state_store(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    given_state_absent = not workspace["state_store"].exists()

    body = _create_episode(client, source_folder)
    episode_id = body["episode_id"]

    assert given_state_absent
    with StateStore.open(workspace["state_store"]) as store:
        snapshot = store.get_job_snapshot(str(body["job_id"]))  # type: ignore[arg-type]
    assert snapshot.job.episode_id == episode_id
    assert snapshot.job.status == "CREATED"
    assert snapshot.job.current_stage == "intake"


def test_create_episode_is_idempotent_per_source_and_conflicts_on_second_create(
    client: TestClient, source_folder: Path
) -> None:
    first = _create_episode(client, source_folder)

    repeat = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "different brief"},
    )

    assert repeat.status_code == 409
    error = repeat.json()["error"]
    assert error["code"] == "episode-exists"
    assert first["episode_id"]


def test_create_episode_rejects_unknown_source_folder(
    client: TestClient, tmp_path: Path
) -> None:
    response = client.post(
        "/episodes",
        json={"source_folder": str(tmp_path / "missing"), "brief_text": "brief"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "source-folder-not-found"


def test_create_episode_rejects_malformed_payload(client: TestClient) -> None:
    response = client.post("/episodes", json={"brief_text": ""})
    assert response.status_code == 422
    assert "error" in response.json()


# ---------------------------------------------------------------------------
# (b) list/detail round-trip with progress shape
# ---------------------------------------------------------------------------


def test_list_and_detail_round_trip_with_progress_shape(
    client: TestClient, source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)
    episode_id = str(created["episode_id"])

    listed = client.get("/episodes")
    assert listed.status_code == 200
    entries = listed.json()["episodes"]
    assert [entry["episode_id"] for entry in entries] == [episode_id]

    detail = client.get(f"/episodes/{episode_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["episode_id"] == episode_id
    assert body["status"] == "CREATED"
    assert body["current_stage"] == "intake"
    assert body["stage_runs"] == []
    assert body["created_at_seq"] >= 1
    assert body["updated_at_seq"] >= 1


def test_list_reflects_stage_run_progress_from_state_store(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)
    episode_id = str(created["episode_id"])

    with StateStore.open(workspace["state_store"]) as store:
        store.record_stage_run(
            StageRunRow(
                job_id=str(created["job_id"]),
                stage_name="ingest",
                idempotency_key="ingest-run-1",
                input_artifact_hashes=(),
                adopted_artifact_hash=None,
                status="succeeded",
            )
        )

    detail = client.get(f"/episodes/{episode_id}")
    runs = detail.json()["stage_runs"]
    assert [run["stage_name"] for run in runs] == ["ingest"]
    assert runs[0]["status"] == "succeeded"


# ---------------------------------------------------------------------------
# (c) brief put/get round-trip
# ---------------------------------------------------------------------------


def test_brief_get_put_round_trip(client: TestClient, source_folder: Path) -> None:
    created = _create_episode(client, source_folder)
    episode_id = str(created["episode_id"])

    initial = client.get(f"/episodes/{episode_id}/brief")
    assert initial.status_code == 200
    assert initial.json() == {
        "episode_id": episode_id,
        "brief_text": "travel vlog, calm pacing",
        "status": "draft",
    }

    updated = client.put(
        f"/episodes/{episode_id}/brief", json={"brief_text": "revised: add food section"}
    )
    assert updated.status_code == 200
    assert updated.json()["brief_text"] == "revised: add food section"
    assert updated.json()["status"] == "draft"

    reread = client.get(f"/episodes/{episode_id}/brief")
    assert reread.json()["brief_text"] == "revised: add food section"


def test_brief_put_rejects_empty_payload(client: TestClient, source_folder: Path) -> None:
    created = _create_episode(client, source_folder)
    response = client.put(
        f"/episodes/{created['episode_id']!s}/brief", json={"brief_text": ""}
    )
    assert response.status_code == 422
    assert "error" in response.json()


# ---------------------------------------------------------------------------
# (d) unknown episode id -> typed structured 404
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/episodes/ep-deadbeef", None),
        ("GET", "/episodes/ep-deadbeef/brief", None),
        ("PUT", "/episodes/ep-deadbeef/brief", {"brief_text": "x"}),
        ("GET", "/episodes/ep-deadbeef/preview", None),
        ("GET", "/episodes/ep-deadbeef/flags", None),
        ("POST", "/episodes/ep-deadbeef/review-chat", {"text": "x"}),
        ("POST", "/episodes/ep-deadbeef/rebuild", {}),
        ("GET", "/episodes/ep-deadbeef/approvals", None),
        (
            "POST",
            "/episodes/ep-deadbeef/approvals/opr-00000001",
            {"decision": "approve", "actor_id": "op-1"},
        ),
        ("GET", "/episodes/ep-deadbeef/publish-status", None),
    ],
)
def test_unknown_episode_yields_structured_404(
    client: TestClient, method: str, path: str, payload: dict[str, object] | None
) -> None:
    response = client.request(method, path, json=payload)
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "episode-not-found"
    assert error["detail"]


def test_episode_id_path_traversal_is_refused(client: TestClient) -> None:
    for bad in ("..%2F..%2Fetc", "ep_%2Fescape"):
        response = client.get(f"/episodes/{bad}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] in {"episode-not-found", "route-not-found"}


# ---------------------------------------------------------------------------
# (e) loopback discipline: factory and run() refuse non-loopback binds
# ---------------------------------------------------------------------------


def test_factory_rejects_non_loopback_bind_host(workspace: dict[str, Path]) -> None:
    for hostile in ("0.0.0.0", "localhost", "::1", "192.168.1.4", "example.invalid"):  # noqa: S104
        with pytest.raises(CockpitBindError) as raised:
            create_cockpit_app(
                state_store_path=workspace["state_store"],
                episodes_root=workspace["episodes_root"],
                bind_host=hostile,
            )
        assert raised.value.code == "bind-not-loopback"


def test_run_refuses_non_loopback_host(workspace: dict[str, Path]) -> None:
    with pytest.raises(CockpitBindError) as raised:
        run(
            state_store_path=workspace["state_store"],
            episodes_root=workspace["episodes_root"],
            host="0.0.0.0",  # noqa: S104
            port=8642,
        )
    assert raised.value.code == "bind-not-loopback"


def test_run_accepts_only_the_loopback_host_constant() -> None:
    assert LOOPBACK_HOST == "127.0.0.1"


# ---------------------------------------------------------------------------
# (f) reference registration registers into the library via the real ingest
# ---------------------------------------------------------------------------


def test_reference_registration_uses_real_ingest(
    client: TestClient, workspace: dict[str, Path], tmp_path: Path
) -> None:
    reference_file = tmp_path / "style-reference.mp4"
    reference_file.write_bytes(b"reference-bytes")

    response = client.post("/references", json={"path": str(reference_file)})
    assert response.status_code == 200
    body = response.json()
    assert body["source_id"].startswith("ref-")
    assert len(body["sha256"]) == 64
    assert body["library_version"] == 2

    library = ReferenceLibraryV1.model_validate_json(
        (workspace["episodes_root"] / "reference-library.json").read_bytes()
    )
    assert [source.source_id for source in library.sources] == [body["source_id"]]
    assert library.version == 2


def test_reference_registration_rejects_missing_file(client: TestClient) -> None:
    response = client.post("/references", json={"path": "/nonexistent/ref.mp4"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "reference-unavailable"


# ---------------------------------------------------------------------------
# (g) review chat accepts and stores the raw message
# ---------------------------------------------------------------------------


def test_review_chat_stores_raw_message(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)
    episode_id = str(created["episode_id"])

    response = client.post(
        f"/episodes/{episode_id}/review-chat",
        json={"text": "この後2秒残して", "at_seconds": 12.5},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["received"] is True
    assert body["sequence"] == 1

    chat_log = (
        workspace["episodes_root"] / episode_id / "review-chat.jsonl"
    ).read_text(encoding="utf-8")
    assert '"text":"' in chat_log
    assert "この後2秒残して" in chat_log

    second = client.post(f"/episodes/{episode_id}/review-chat", json={"text": "次のテイクで"})
    assert second.json()["sequence"] == 2


# ---------------------------------------------------------------------------
# (h) preview: structured 404 when absent, FileResponse when present
# ---------------------------------------------------------------------------


def test_preview_absent_is_structured_404(
    client: TestClient, source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)
    response = client.get(f"/episodes/{created['episode_id']!s}/preview")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "preview-not-found"


def test_preview_serves_existing_file(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)
    preview_dir = workspace["episodes_root"] / str(created["episode_id"]) / "previews"
    preview_dir.mkdir(parents=True)
    (preview_dir / PREVIEW_NAME).write_bytes(b"mp4-bytes")

    response = client.get(f"/episodes/{created['episode_id']!s}/preview")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("video/mp4")
    assert response.content == b"mp4-bytes"


# ---------------------------------------------------------------------------
# stubs with explicit not-available contracts
# ---------------------------------------------------------------------------


def test_flags_stub_reports_not_yet_generated(client: TestClient, source_folder: Path) -> None:
    created = _create_episode(client, source_folder)
    response = client.get(f"/episodes/{created['episode_id']!s}/flags")
    assert response.status_code == 200
    body = response.json()
    assert body["flags"] == []
    assert body["not_yet_generated"] is True


def test_publish_status_explicitly_unavailable(client: TestClient, source_folder: Path) -> None:
    created = _create_episode(client, source_folder)
    response = client.get(f"/episodes/{created['episode_id']!s}/publish-status")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["reason"]


def test_rebuild_records_intent_without_scheduling(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)
    episode_id = str(created["episode_id"])

    response = client.post(f"/episodes/{episode_id}/rebuild", json={"stage_hint": "preview"})
    assert response.status_code == 202
    body = response.json()
    assert body["stage_hint"] == "preview"
    assert body["scheduled"] is False

    log = (workspace["episodes_root"] / episode_id / "rebuild-requests.jsonl").read_text(
        encoding="utf-8"
    )
    assert "preview" in log


# ---------------------------------------------------------------------------
# approvals: read from the existing append-only store, append via its API
# ---------------------------------------------------------------------------


def _seed_approval_store(
    workspace: dict[str, Path], episode_id: str, target_hash: str
) -> None:
    records_path = (
        workspace["episodes_root"] / episode_id / "approvals" / "records.jsonl"
    )
    store = OperationRecordStore(records_path)
    draft = record_fixture_operation(
        purpose="editorial",
        target_bundle_hash=target_hash,
        decision="reject",
        actor_id="operator-1",
    )
    store.append(draft)


def test_approvals_list_and_execute_round_trip(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)
    episode_id = str(created["episode_id"])
    target_hash = "a" * 64
    _seed_approval_store(workspace, episode_id, target_hash)

    listed = client.get(f"/episodes/{episode_id}/approvals")
    assert listed.status_code == 200
    body = listed.json()
    assert body["available"] is True
    assert len(body["approvals"]) == 1
    approval_id = body["approvals"][0]["record_id"]

    executed = client.post(
        f"/episodes/{episode_id}/approvals/{approval_id}",
        json={"decision": "approve", "actor_id": "operator-1"},
    )
    assert executed.status_code == 200
    executed_body = executed.json()
    assert executed_body["decision"] == "approve"
    assert executed_body["runner_class"] == "automation"
    assert executed_body["fixture_only"] is True

    latest = OperationRecordStore(
        workspace["episodes_root"] / episode_id / "approvals" / "records.jsonl"
    ).latest_records()
    assert latest[("editorial", target_hash)].decision == "approve"


def test_approvals_list_reports_unavailable_when_store_absent(
    client: TestClient, source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)
    response = client.get(f"/episodes/{created['episode_id']!s}/approvals")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["approvals"] == []


def test_approval_execute_unknown_record_is_structured_404(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)
    episode_id = str(created["episode_id"])
    _seed_approval_store(workspace, episode_id, "b" * 64)

    response = client.post(
        f"/episodes/{episode_id}/approvals/opr-99999999",
        json={"decision": "approve", "actor_id": "operator-1"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "approval-not-found"


# ---------------------------------------------------------------------------
# state-authority invariant: cockpit reads back through the real store
# ---------------------------------------------------------------------------


def test_create_then_status_read_back_via_real_state_store(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    created = _create_episode(client, source_folder)

    with StateStore.open(workspace["state_store"]) as store:
        snapshot = store.get_job_snapshot(str(created["job_id"]))  # type: ignore[arg-type]

    detail = client.get(f"/episodes/{created['episode_id']!s}")
    assert detail.json()["status"] == snapshot.job.status
    assert detail.json()["updated_at_seq"] == snapshot.job.updated_at_seq

    with pytest.raises(StateStoreError), StateStore.open(workspace["state_store"]) as store:
        store.get_job_snapshot("ep-does-not-exist")
