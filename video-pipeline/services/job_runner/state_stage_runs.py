"""Stage-run bookkeeping operations for the runtime StateStore.

Mixin over a shared ``sqlite3.Connection``. ``record_stage_run`` is the
idempotency gate: the same ``(job_id, idempotency_key)`` with the same
adopted hash replays without effect, a different adopted hash (or
different recorded inputs/stage for the same key) is a conflict, and
only a previously-unadopted row may adopt an artifact hash.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from services.job_runner.state_context import StateContext
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_models import (
    STAGE_RUN_COLUMNS,
    StageRunRow,
    StageRunStatus,
    decode_hash_list,
    encode_hash_list,
    int_column,
    text_column,
)

if TYPE_CHECKING:
    from services.contracts.primitives import Identifier


class StageRunOps(StateContext):
    def _select_stage_run(self, job_id: str, idempotency_key: str) -> StageRunRow | None:
        row = self._connection.execute(
            f"SELECT {STAGE_RUN_COLUMNS} FROM stage_runs"  # noqa: S608 (constant columns)
            " WHERE job_id = ? AND idempotency_key = ?",
            (job_id, idempotency_key),
        ).fetchone()
        return None if row is None else stage_run_from_row(row)

    def record_stage_run(self, run: StageRunRow) -> StageRunRow:
        self._require_job(run.job_id)
        existing = self._select_stage_run(run.job_id, run.idempotency_key)
        if existing is not None:
            _require_replayable(existing, run)
            if existing == run:
                return existing
        self._connection.execute(
            # Upsert clock contract (2P): first_started_at freezes the first
            # entry into running; first_output_arrived_at survives every later
            # write once set; last_transition_at moves ONLY when the row's
            # meaning (status/retry/error/adopted hash) changes, so a
            # re-asserted identical row is never mistaken for progress.
            f"INSERT INTO stage_runs ({STAGE_RUN_COLUMNS})"  # noqa: S608 (constant columns)
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(job_id, idempotency_key) DO UPDATE SET"
            " stage_name = excluded.stage_name,"
            " input_artifact_hashes = excluded.input_artifact_hashes,"
            " adopted_artifact_hash = excluded.adopted_artifact_hash,"
            " status = excluded.status, retry_count = excluded.retry_count,"
            " last_error_code = excluded.last_error_code,"
            " run_id = COALESCE(excluded.run_id, stage_runs.run_id),"
            " first_started_at = CASE WHEN excluded.status = 'running' THEN"
            " COALESCE(stage_runs.first_started_at, excluded.first_started_at)"
            " ELSE stage_runs.first_started_at END,"
            " first_output_arrived_at = CASE WHEN excluded.status = 'succeeded' THEN"
            " COALESCE(stage_runs.first_output_arrived_at, excluded.first_output_arrived_at)"
            " ELSE stage_runs.first_output_arrived_at END,"
            " last_transition_at = CASE WHEN"
            " stage_runs.status IS NOT excluded.status"
            " OR stage_runs.retry_count IS NOT excluded.retry_count"
            " OR stage_runs.last_error_code IS NOT excluded.last_error_code"
            " OR stage_runs.adopted_artifact_hash IS NOT excluded.adopted_artifact_hash"
            " THEN COALESCE(excluded.last_transition_at, stage_runs.last_transition_at)"
            " ELSE stage_runs.last_transition_at END",
            (
                run.job_id,
                run.stage_name,
                run.idempotency_key,
                encode_hash_list(run.input_artifact_hashes),
                run.adopted_artifact_hash,
                run.status,
                run.retry_count,
                run.last_error_code,
                run.run_id,
                run.first_started_at,
                run.first_output_arrived_at,
                run.last_transition_at,
            ),
        )
        result = self._select_stage_run(run.job_id, run.idempotency_key)
        if result is None:
            raise StateStoreError("stage-run-missing", "stage run vanished mid-write")
        return result

    def bump_retry(
        self,
        *,
        job_id: Identifier,
        idempotency_key: Identifier,
        error_code: Identifier,
        last_transition_at: str | None = None,
    ) -> StageRunRow:
        self._require_stage_run(job_id, idempotency_key)
        self._connection.execute(
            "UPDATE stage_runs SET retry_count = retry_count + 1, last_error_code = ?,"
            " last_transition_at = COALESCE(?, last_transition_at)"
            " WHERE job_id = ? AND idempotency_key = ?",
            (error_code, last_transition_at, job_id, idempotency_key),
        )
        return self._read_stage_run(job_id, idempotency_key)

    def set_stage_status(
        self,
        *,
        job_id: Identifier,
        idempotency_key: Identifier,
        status: StageRunStatus,
        last_error_code: Identifier | None = None,
        last_transition_at: str | None = None,
    ) -> StageRunRow:
        self._require_stage_run(job_id, idempotency_key)
        self._connection.execute(
            "UPDATE stage_runs SET status = ?, last_error_code = ?,"
            " last_transition_at = COALESCE(?, last_transition_at)"
            " WHERE job_id = ? AND idempotency_key = ?",
            (status, last_error_code, last_transition_at, job_id, idempotency_key),
        )
        return self._read_stage_run(job_id, idempotency_key)

    def all_stage_runs(self) -> tuple[StageRunRow, ...]:
        rows = self._connection.execute(
            f"SELECT {STAGE_RUN_COLUMNS} FROM stage_runs ORDER BY rowid"  # noqa: S608 (constant columns)
        ).fetchall()
        return tuple(stage_run_from_row(row) for row in rows)

    def _require_stage_run(self, job_id: str, idempotency_key: str) -> None:
        if self._select_stage_run(job_id, idempotency_key) is None:
            raise StateStoreError(
                "stage-run-missing", f"no stage run for {job_id}/{idempotency_key}"
            )

    def _read_stage_run(self, job_id: str, idempotency_key: str) -> StageRunRow:
        row = self._select_stage_run(job_id, idempotency_key)
        if row is None:
            raise StateStoreError("stage-run-missing", "stage run vanished mid-write")
        return row


def _require_replayable(existing: StageRunRow, run: StageRunRow) -> None:
    if (
        existing.stage_name != run.stage_name
        or existing.input_artifact_hashes != run.input_artifact_hashes
    ):
        raise StateStoreError(
            "stage-run-conflict",
            f"idempotency key {run.idempotency_key} already recorded stage"
            f" {existing.stage_name} with inputs {existing.input_artifact_hashes};"
            f" refusing {run.stage_name} with {run.input_artifact_hashes}",
        )
    # Only a previously-unadopted row may adopt an artifact hash; an adopted
    # row rejects ANY different hash (None included) — a later re-run of the
    # same stage happens under a NEW run_id, hence a NEW idempotency key.
    if (
        existing.adopted_artifact_hash is not None
        and run.adopted_artifact_hash != existing.adopted_artifact_hash
    ):
        raise StateStoreError(
            "stage-run-conflict",
            f"idempotency key {run.idempotency_key} already adopted"
            f" {existing.adopted_artifact_hash}, refusing {run.adopted_artifact_hash}",
        )


def stage_run_from_row(row: Sequence[object]) -> StageRunRow:
    return StageRunRow(
        job_id=text_column(row[0]),
        stage_name=text_column(row[1]),
        idempotency_key=text_column(row[2]),
        input_artifact_hashes=decode_hash_list(text_column(row[3])),
        adopted_artifact_hash=None if row[4] is None else text_column(row[4]),
        status=cast("StageRunStatus", text_column(row[5])),
        retry_count=int_column(row[6]),
        last_error_code=None if row[7] is None else text_column(row[7]),
        run_id=None if row[8] is None else text_column(row[8]),
        first_started_at=None if row[9] is None else text_column(row[9]),
        first_output_arrived_at=None if row[10] is None else text_column(row[10]),
        last_transition_at=None if row[11] is None else text_column(row[11]),
    )


__all__ = ["StageRunOps", "stage_run_from_row"]
