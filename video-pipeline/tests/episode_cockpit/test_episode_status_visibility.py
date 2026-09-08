"""工程2P: episode_status run-scoped visibility — complementary U30 cases.

Extends ``test_run_visibility.py`` (which covers the apply-flow U30)
with: a plain-intent reservation, a legacy row written BEFORE the new
columns (run_id absent = honest unmeasured), the worker report scoped
across TWO runs with mixed old-format log lines (lines without run_id
never leak into the newer run's scope), and current-run retry scoping
(past-run residual retries never surface).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.episode_cockpit.app import create_cockpit_app
from services.episode_cockpit.models import RebuildRequestEntry
from services.foundation_io import canonical_model_bytes
from services.job_runner.state_models import StageRunRow
from services.job_runner.state_store import StateStore

T2 = "2026-09-08T10:00:01+00:00"
T3 = "2026-09-08T10:00:02+00:00"
T4 = "2026-09-08T10:00:03+00:00"
T5 = "2026-09-08T10:00:04+00:00"
OLD_RUN, NEW_RUN = "oldrun01", "newrun02"


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


def _seed_row(  # noqa: PLR0913 (the seed fields ARE the row contract being pinned)
    workspace: dict[str, Path],
    episode_id: str,
    *,
    run_id: str | None,
    stage: str,
    status: str,
    retry_count: int = 0,
    recorded_at: str | None = None,
) -> None:
    clock: dict[str, object] = (
        {} if recorded_at is None
        else {"last_transition_at": recorded_at,
              **({"first_output_arrived_at": recorded_at} if status == "succeeded" else {}),
              **({"first_started_at": recorded_at} if status == "running" else {})}
    )
    with StateStore.open(workspace["state_store"]) as store:
        store.record_stage_run(
            StageRunRow(
                job_id=episode_id,
                stage_name=stage,
                input_artifact_hashes=(),
                status=status,  # type: ignore[arg-type]
                idempotency_key=f"cockpit-episode-runner-v1:{run_id or 'legacy'}:{stage}",
                run_id=run_id,
                retry_count=retry_count,
                **clock,  # type: ignore[arg-type]
            )
        )


def _append_rebuild_entry(
    workspace: dict[str, Path], episode_id: str, **fields: object
) -> None:
    path = workspace["episodes_root"] / episode_id / "rebuild-requests.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_bytes() if path.is_file() else b""
    entry = RebuildRequestEntry(
        sequence=existing.count(b"\n") + 1,
        **fields,  # type: ignore[arg-type]
    )
    with path.open("ab") as stream:
        stream.write(canonical_model_bytes(entry) + b"\n")


def _rows_by_stage(body: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(row["stage_name"]): row for row in body["stage_runs"]}  # type: ignore[index]


def test_u30_plain_reservation_with_old_run_residual_reads_as_reserved(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    for stage in ("plan", "compile"):
        _seed_row(
            workspace, episode_id, run_id=OLD_RUN, stage=stage, status="succeeded",
            recorded_at=T2,
        )
    _seed_row(  # the honest crash residual of the OLD run
        workspace, episode_id, run_id=OLD_RUN, stage="preview", status="running",
        recorded_at=T2,
    )
    _seed_row(  # a pre-工程2P row: nothing measured beyond the old columns
        workspace, episode_id, run_id=None, stage="analyze", status="succeeded",
    )
    reserved = client.post(
        f"/episodes/{episode_id}/rebuild", json={"stage_hint": "plan,compile,preview"}
    )
    assert reserved.status_code == 202

    body = client.get(f"/episodes/{episode_id}").json()
    rows = _rows_by_stage(body)

    assert rows["compile"]["run_id"] == OLD_RUN  # rows expose their run
    assert rows["preview"]["run_id"] == OLD_RUN
    assert rows["preview"]["status"] == "running"  # residual stays visible...
    assert "run_id" not in rows["analyze"]  # ...legacy rows stay honestly unmeasured
    assert body["current_run"] is None  # latest chain entry is UNSPAWNED
    assert body["current_run"] != OLD_RUN  # the old run never reads as current
    latest = body["rebuild_requests"][-1]
    assert latest["stage_hint"] == "plan,compile,preview"
    assert latest["spawned"] is False
    assert latest["run_id"] is None  # 未起動 readable
    running = [row for row in body["stage_runs"] if row["status"] == "running"]
    assert all(row.get("run_id") != body["current_run"] for row in running)
    assert "preview_first_arrived_at" not in body  # NOT derivable as 完了


def test_worker_report_is_scoped_to_the_current_run(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_row(workspace, episode_id, run_id=NEW_RUN, stage="preview", status="succeeded")
    log = workspace["episodes_root"] / episode_id / "runner.log"
    log.write_text(
        "\n".join(
            json.dumps(payload, ensure_ascii=False)
            for payload in (
                {"ts": T2, "event": "runner_started", "run_id": OLD_RUN},
                {"ts": T3, "event": "stage", "stage": "ingest"},  # old line: no run_id
                {"ts": T4, "event": "runner_started", "run_id": NEW_RUN},
                {"ts": T5, "event": "stage", "stage": "preview", "run_id": NEW_RUN},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _append_rebuild_entry(
        workspace, episode_id, stage_hint="preview", spawned=True, run_id=NEW_RUN
    )

    body = client.get(f"/episodes/{episode_id}").json()

    assert body["current_run"] == NEW_RUN
    assert body["last_worker_report_at"] == T5  # terminal/failed events count too
    assert body["last_worker_report_event"] == "stage"


def test_retry_state_scopes_to_the_current_run(
    client: TestClient, workspace: dict[str, Path], source_folder: Path
) -> None:
    episode_id = _create_episode(client, source_folder)
    _seed_row(  # past-run residual retry: must NOT surface
        workspace, episode_id, run_id=OLD_RUN, stage="preview", status="running",
        retry_count=3, recorded_at=T2,
    )
    _seed_row(
        workspace, episode_id, run_id=NEW_RUN, stage="preview", status="running",
        retry_count=2, recorded_at=T3,
    )
    _append_rebuild_entry(
        workspace, episode_id, stage_hint="preview", spawned=True, run_id=NEW_RUN
    )

    body = client.get(f"/episodes/{episode_id}").json()

    assert body["current_run"] == NEW_RUN
    assert body["current_run_retry_count"] == 2
