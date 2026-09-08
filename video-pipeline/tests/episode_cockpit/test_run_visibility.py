"""2P run/waiting visibility (U30 backend half): production-path scoping.

Complementary to the sibling 2P files: this drives the REAL runner and
rebuild flows (fake chain; real StateStore/runner.log/rebuild jsonl
writes) and pins the payload contract end-to-end — an old run's success
and its ``running``残留 can never read as current progress or completion
once a newer rebuild is only RESERVED (codex U30 case).
"""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.cli import episode_runner
from services.cli.episode_runner_state import RunContext, record_stage
from services.episode_cockpit import episode_ops
from services.episode_cockpit.app import create_cockpit_app
from services.job_runner.migrations import MIGRATIONS, apply_migrations
from services.job_runner.state_store import StateStore
from tests.episode_cockpit.test_rebuild_executor import (
    _fake_chain_factory,
)

REMOVE_TEXT = "1秒のところを削除して"
TS2 = "2026-09-08T00:00:02+00:00"
TS3 = "2026-09-08T00:00:03+00:00"
FAKE_CHAIN = _fake_chain_factory({})


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


def _initial_run(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
) -> tuple[str, Path]:
    monkeypatch.delenv("EDITORIAL_RUNTIME_CONFIG", raising=False)
    monkeypatch.setattr(episode_runner, "run_real_chain", FAKE_CHAIN)
    response = client.post(
        "/episodes",
        json={"source_folder": str(source_folder), "brief_text": "visibility"},
    )
    assert response.status_code == 200
    episode_id = str(response.json()["episode_id"])
    episode_dir = workspace["episodes_root"] / episode_id
    code = episode_runner.run(
        episode_root=episode_dir, stop="PREVIEW_READY",
        state_store_path=workspace["state_store"], run_id=run_id,
    )
    assert code == episode_runner.EXIT_SUCCESS
    return episode_id, episode_dir


def _apply_remove(client: TestClient, episode_id: str) -> dict[str, object]:
    preview = client.post(
        f"/episodes/{episode_id}/review-chat", json={"text": REMOVE_TEXT, "at_seconds": None}
    )
    assert preview.status_code == 200
    response = client.post(
        f"/episodes/{episode_id}/review-chat/apply",
        json={"text": REMOVE_TEXT, "at_seconds": None},
    )
    assert response.status_code == 200
    return response.json()["applied"]  # type: ignore[no-any-return]


# --- old stores keep serving; the API stays honest about unmeasured ------


def test_old_store_without_new_columns_still_serves(
    workspace: dict[str, Path], client: TestClient
) -> None:
    connection = sqlite3.connect(workspace["state_store"])
    apply_migrations(connection, MIGRATIONS[:2])  # pre-工程2P schema (v2)
    connection.execute(
        "INSERT INTO jobs (job_id, episode_id, current_stage, status,"
        " created_at_seq, updated_at_seq) VALUES ('ep-old','ep-old','preview',"
        "'PREVIEW_READY',1,1)"
    )
    connection.execute(
        "INSERT INTO stage_runs (job_id, stage_name, idempotency_key,"
        " input_artifact_hashes, adopted_artifact_hash, status, retry_count,"
        " last_error_code) VALUES ('ep-old','preview','v1:run-old:preview',"
        "'[]',NULL,'running',0,NULL)"
    )
    connection.commit()
    connection.close()

    with StateStore.open(workspace["state_store"]) as store:
        row = store.get_job_snapshot("ep-old").stage_runs[0]
    assert row.run_id is None
    assert row.first_started_at is None
    assert row.last_transition_at is None
    assert row.first_output_arrived_at is None

    status = client.get("/episodes/ep-old").json()
    stage = status["stage_runs"][0]
    for absent in ("run_id", "first_started_at", "last_transition_at",
                   "first_output_arrived_at"):
        assert absent not in stage  # absent field = unmeasured, honest
    assert status["status"] == "PREVIEW_READY"
    assert status["current_run"] is None
    assert status["last_worker_report_at"] is None
    assert status["unreviewed_proposal_set"] is False


# --- U30: old-run success + old-run running残留 + new rebuild 予約済み ---


def test_u30_current_run_is_not_the_old_run_and_reservation_shows(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, _ = _initial_run(
        client, workspace, source_folder, monkeypatch, run_id="run-old"
    )
    applied = _apply_remove(client, episode_id)
    first = client.post(
        f"/episodes/{episode_id}/rebuild",
        json={"applied_command": applied["command_id"]},
    )
    assert first.status_code == 202
    spawned_run = str(first.json()["run_id"])
    assert spawned_run != "run-old"
    with StateStore.open(workspace["state_store"]) as store:
        ctx = RunContext(
            workspace["state_store"], episode_id, spawned_run, "PREVIEW_READY", io.BytesIO()
        )
        record_stage(store, ctx, "plan", "succeeded", adopted="b" * 64, now=TS2)
        record_stage(store, ctx, "compile", "succeeded", adopted="c" * 64, now=TS2)
        record_stage(store, ctx, "preview", "running", now=TS3)  # 残留 frontier

    class _ExplodingShim:
        Popen = staticmethod(lambda *_a, **_k: (_ for _ in ()).throw(OSError("no")))

    monkeypatch.setattr(episode_ops, "subprocess", _ExplodingShim)
    reserved = client.post(
        f"/episodes/{episode_id}/rebuild",
        json={"applied_command": applied["command_id"]},
    )
    assert reserved.status_code == 422

    status = client.get(f"/episodes/{episode_id}").json()
    assert status["status"] == "PREVIEW_READY"  # job semantics unchanged
    rows = {(row["stage_name"], row["run_id"]): row
            for row in status["stage_runs"] if "run_id" in row}
    assert rows[("preview", spawned_run)]["status"] == "running"  # 残留 stays visible
    assert rows[("preview", "run-old")]["first_output_arrived_at"]
    assert status["current_run"] is None  # latest chain entry is UNSPAWNED
    latest = status["rebuild_requests"][-1]
    assert latest["spawned"] is False
    assert latest["run_id"] is None  # 未起動 readable without inference
    assert latest["target_version"] == "v2"  # 予約対象版 is explicit
    pending = status["pending_rebuild"]
    assert pending["run_id"] is None
    assert pending["run_id"] != rows[("preview", spawned_run)]["run_id"]
    assert pending["run_id"] != rows[("preview", "run-old")]["run_id"]
    assert "preview_first_arrived_at" not in status  # no current-run output


# --- worker report from REAL log lines, scoped to the current run --------


def test_last_worker_report_scoped_to_current_run(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, episode_dir = _initial_run(
        client, workspace, source_folder, monkeypatch, run_id="run-init"
    )
    lines = [json.loads(line) for line in
             (episode_dir / "runner.log").read_text(encoding="utf-8").splitlines()
             if line.startswith("{")]
    reports = [event for event in lines if event.get("run_id") == "run-init"]
    status = client.get(f"/episodes/{episode_id}").json()
    assert status["current_run"] == "run-init"
    assert status["last_worker_report_at"] == reports[-1]["ts"]
    assert status["last_worker_report_event"] == reports[-1]["event"]
    assert status["intake_created_at"]
    assert status["preview_first_arrived_at"]  # this-run preview arrival


def test_initial_run_target_version_from_complete_publish_record(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episode_id, _ = _initial_run(
        client, workspace, source_folder, monkeypatch, run_id="run-init"
    )

    status = client.get(f"/episodes/{episode_id}").json()

    assert status["current_run"] == "run-init"
    assert status["current_target_version"] == "v1"


# --- rebuild linkage: 予約(sequence)→起動(run_id) survives reload ---------


def test_rebuild_linkage_chain_readable_after_reload(
    client: TestClient,
    workspace: dict[str, Path],
    source_folder: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_spawn_calls: list[dict[str, object]],
) -> None:
    episode_id, _ = _initial_run(
        client, workspace, source_folder, monkeypatch, run_id="run-old"
    )
    applied = _apply_remove(client, episode_id)
    response = client.post(
        f"/episodes/{episode_id}/rebuild",
        json={"applied_command": applied["command_id"]},
    )
    assert response.status_code == 202
    spawned_run = str(response.json()["run_id"])
    argv = [str(a) for a in runner_spawn_calls[-1]["argv"]]  # type: ignore[arg-type]
    assert argv[argv.index("--run-id") + 1] == spawned_run

    reloaded = create_cockpit_app(
        state_store_path=workspace["state_store"],
        episodes_root=workspace["episodes_root"],
    )
    with TestClient(reloaded) as fresh_client:
        status = fresh_client.get(f"/episodes/{episode_id}").json()
    assert status["current_run"] == spawned_run
    chain = [(entry["sequence"], entry["spawned"], entry["run_id"])
             for entry in status["rebuild_requests"]]
    assert chain == [(1, False, None), (2, True, spawned_run)]
