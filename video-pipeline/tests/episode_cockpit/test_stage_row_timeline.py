"""工程2P: stage-row wall-clock semantics in the runtime StateStore.

Three SEPARATED meanings (codex fix condition #2): ``first_started_at``
is the FIRST time the row entered ``running``; ``last_transition_at``
moves when the row's MEANING (status/retry/error/hash) changes — a
re-asserted identical row is never mistaken for progress;
``first_output_arrived_at`` is the FIRST time the row entered
``succeeded`` and an upsert MUST NOT overwrite it once set. A running
start never sets first-output. Old stores (rows written before the new
columns existed) still load and serve — absent fields are honest
unmeasured values, never fabricated.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.job_runner.migrations import MIGRATIONS, apply_migrations
from services.job_runner.state_models import StageRunRow
from services.job_runner.state_store import StateStore

if TYPE_CHECKING:
    from collections.abc import Iterator

T1, T2, T3 = (f"2026-09-08T10:00:0{index}+00:00" for index in range(1, 4))
KEY = "cockpit-episode-runner-v1:run01:preview"


def _row(stage: str, *, status: str, key: str | None = None, **extra: object) -> StageRunRow:
    return StageRunRow(
        job_id="ep-run",
        stage_name=stage,
        input_artifact_hashes=(),
        status=status,  # type: ignore[arg-type]
        idempotency_key=key if key is not None else f"cockpit-episode-runner-v1:run01:{stage}",
        **extra,  # type: ignore[arg-type]
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[StateStore]:
    with StateStore.open(tmp_path / "state.db") as opened:
        opened.create_job(job_id="ep-run", episode_id="ep-run", current_stage="intake")
        yield opened


def test_running_start_sets_first_started_but_never_first_output(
    store: StateStore,
) -> None:
    store.record_stage_run(
        _row("preview", status="running", first_started_at=T1, last_transition_at=T1)
    )

    (row,) = store.get_job_snapshot("ep-run").stage_runs
    assert row.first_started_at == T1
    assert row.last_transition_at == T1
    assert row.first_output_arrived_at is None  # running is NOT output progress


def test_first_output_survives_reassert_and_meaning_changes(store: StateStore) -> None:
    store.record_stage_run(
        _row("preview", status="running", first_started_at=T1, last_transition_at=T1)
    )
    store.record_stage_run(
        _row(
            "preview", status="succeeded", adopted_artifact_hash="a" * 64,
            first_output_arrived_at=T2, last_transition_at=T2,
        )
    )
    # identical re-assert with a LATER stamp: not progress — nothing moves
    store.record_stage_run(
        _row(
            "preview", status="succeeded", adopted_artifact_hash="a" * 64,
            first_output_arrived_at=T3, last_transition_at=T3,
        )
    )
    (row,) = store.get_job_snapshot("ep-run").stage_runs
    assert row.first_started_at == T1
    assert row.first_output_arrived_at == T2  # first arrival NEVER overwritten
    assert row.last_transition_at == T2  # meaning unchanged ⇒ no transition

    # a real meaning change on a NOT-yet-adopted row moves last_transition only
    store.record_stage_run(
        _row("compile", status="running", first_started_at=T1, last_transition_at=T1)
    )
    store.record_stage_run(
        _row(
            "compile", status="failed_blocked", last_error_code="boom",
            last_transition_at=T2,
        )
    )
    (blocked,) = [
        run for run in store.get_job_snapshot("ep-run").stage_runs
        if run.stage_name == "compile"
    ]
    assert blocked.first_output_arrived_at is None  # never any output
    assert blocked.first_started_at == T1
    assert blocked.last_transition_at == T2


def test_identical_replay_returns_row_unchanged(store: StateStore) -> None:
    first = store.record_stage_run(
        _row(
            "preview", status="succeeded", adopted_artifact_hash="a" * 64,
            first_output_arrived_at=T2, last_transition_at=T2,
        )
    )
    replayed = store.record_stage_run(
        _row(
            "preview", status="succeeded", adopted_artifact_hash="a" * 64,
            first_output_arrived_at=T2, last_transition_at=T2,
        )
    )

    assert replayed == first


def test_run_id_is_stored_explicitly(store: StateStore) -> None:
    store.record_stage_run(
        StageRunRow(
            job_id="ep-run",
            stage_name="preview",
            input_artifact_hashes=(),
            status="succeeded",  # type: ignore[arg-type]
            idempotency_key=KEY,
            run_id="run042abc123",
        ),
    )

    (row,) = store.get_job_snapshot("ep-run").stage_runs
    assert row.run_id == "run042abc123"


def test_old_store_without_new_columns_still_serves(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        apply_migrations(connection, MIGRATIONS[:2])  # pre-工程2P schema
        connection.execute(
            "INSERT INTO jobs (job_id, episode_id, current_stage, status,"
            " created_at_seq, updated_at_seq) VALUES ('ep-run', 'ep-run',"
            " 'preview', 'PREVIEW_READY', 1, 2)"
        )
        connection.execute(
            "INSERT INTO stage_runs (job_id, stage_name, idempotency_key,"
            " input_artifact_hashes, adopted_artifact_hash, status, retry_count,"
            " last_error_code) VALUES ('ep-run', 'preview', ?, '[]', NULL,"
            " 'succeeded', 0, NULL)",
            (KEY,),
        )
    finally:
        connection.close()

    with StateStore.open(path) as store:
        (row,) = store.get_job_snapshot("ep-run").stage_runs
        assert row.first_started_at is None  # honest: unmeasured, not fabricated
        assert row.last_transition_at is None
        assert row.first_output_arrived_at is None
        assert row.run_id is None
        updated = store.record_stage_run(
            _row(
                "preview", status="succeeded", key=KEY,
                first_output_arrived_at=T2, last_transition_at=T2,
            )
        )
        assert updated.first_output_arrived_at == T2
