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
            "SELECT job_id, stage_name, idempotency_key, input_artifact_hashes,"
            " adopted_artifact_hash, status, retry_count, last_error_code"
            " FROM stage_runs WHERE job_id = ? AND idempotency_key = ?",
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
            "INSERT INTO stage_runs (job_id, stage_name, idempotency_key,"
            " input_artifact_hashes, adopted_artifact_hash, status, retry_count,"
            " last_error_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(job_id, idempotency_key) DO UPDATE SET"
            " stage_name = excluded.stage_name,"
            " input_artifact_hashes = excluded.input_artifact_hashes,"
            " adopted_artifact_hash = excluded.adopted_artifact_hash,"
            " status = excluded.status, retry_count = excluded.retry_count,"
            " last_error_code = excluded.last_error_code",
            (
                run.job_id,
                run.stage_name,
                run.idempotency_key,
                encode_hash_list(run.input_artifact_hashes),
                run.adopted_artifact_hash,
                run.status,
                run.retry_count,
                run.last_error_code,
            ),
        )
        result = self._select_stage_run(run.job_id, run.idempotency_key)
        if result is None:
            raise StateStoreError("stage-run-missing", "stage run vanished mid-write")
        return result

    def bump_retry(
        self, *, job_id: Identifier, idempotency_key: Identifier, error_code: Identifier
    ) -> StageRunRow:
        self._require_stage_run(job_id, idempotency_key)
        self._connection.execute(
            "UPDATE stage_runs SET retry_count = retry_count + 1, last_error_code = ?"
            " WHERE job_id = ? AND idempotency_key = ?",
            (error_code, job_id, idempotency_key),
        )
        return self._read_stage_run(job_id, idempotency_key)

    def set_stage_status(
        self,
        *,
        job_id: Identifier,
        idempotency_key: Identifier,
        status: StageRunStatus,
        last_error_code: Identifier | None = None,
    ) -> StageRunRow:
        self._require_stage_run(job_id, idempotency_key)
        self._connection.execute(
            "UPDATE stage_runs SET status = ?, last_error_code = ?"
            " WHERE job_id = ? AND idempotency_key = ?",
            (status, last_error_code, job_id, idempotency_key),
        )
        return self._read_stage_run(job_id, idempotency_key)

    def all_stage_runs(self) -> tuple[StageRunRow, ...]:
        rows = self._connection.execute(
            "SELECT job_id, stage_name, idempotency_key, input_artifact_hashes,"
            " adopted_artifact_hash, status, retry_count, last_error_code"
            " FROM stage_runs ORDER BY rowid"
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
    )


__all__ = ["StageRunOps", "stage_run_from_row"]
