"""SQLite runtime-state store for the job runner Control Plane.

Discipline (PRD 7.3): this database holds ONLY runtime/control state —
job progress, stage-run bookkeeping, SQL lease rows, approval
references and cache pointers. Committed Plan bodies and every
editorial Artifact remain canonical FILES in the content-addressed
artifact store; rows carry hash pointers only. Lease and sequence
methods take caller-supplied logical ``now``/clock values, never the
wall clock.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Self, cast

from services.job_runner.migrations import (
    LATEST_VERSION,
    MigrationError,
    applied_version,
    apply_migrations,
)
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_leases import LeaseOps
from services.job_runner.state_models import (
    STAGE_RUN_COLUMNS,
    JobRow,
    JobSnapshot,
    JobStatus,
    PragmaState,
    int_column,
    text_column,
)
from services.job_runner.state_pointers import PointerOps
from services.job_runner.state_stage_runs import StageRunOps, stage_run_from_row

if TYPE_CHECKING:
    from services.contracts.primitives import Identifier


class StateStore(LeaseOps, StageRunOps, PointerOps):
    """Context-managed runtime-state connection over one SQLite file."""

    _connection: sqlite3.Connection

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> Self:
        """Open (creating if needed) and migrate the runtime-state DB.

        Refuses to open databases at an unknown schema version or with a
        tampered ``schema_migrations`` history.
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        apply_migrations(connection)
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def pragma_state(self) -> PragmaState:
        journal_mode = str(self._connection.execute("PRAGMA journal_mode").fetchone()[0])
        foreign_keys = int(self._connection.execute("PRAGMA foreign_keys").fetchone()[0])
        synchronous = int(self._connection.execute("PRAGMA synchronous").fetchone()[0])
        return PragmaState(
            journal_mode=journal_mode,
            foreign_keys=foreign_keys == 1,
            synchronous=synchronous,
        )

    def applied_schema_version(self) -> int:
        return applied_version(self._connection)

    def _next_seq(self) -> int:
        row = self._connection.execute(
            "SELECT next_seq FROM state_clock WHERE id = 1"
        ).fetchone()
        if row is None:
            raise StateStoreError("state-clock-missing", "state_clock row 1 is absent")
        seq = int(row[0])
        self._connection.execute(
            "UPDATE state_clock SET next_seq = ? WHERE id = 1", (seq + 1,)
        )
        return seq

    def create_job(
        self,
        *,
        job_id: Identifier,
        episode_id: Identifier,
        current_stage: Identifier,
        status: JobStatus = "CREATED",
    ) -> JobRow:
        existing = self._connection.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if existing is not None:
            raise StateStoreError("job-exists", f"job {job_id} already exists")
        seq = self._next_seq()
        row = JobRow(
            job_id=job_id,
            episode_id=episode_id,
            current_stage=current_stage,
            status=status,
            created_at_seq=seq,
            updated_at_seq=seq,
        )
        self._connection.execute(
            "INSERT INTO jobs (job_id, episode_id, current_stage, status,"
            " created_at_seq, updated_at_seq) VALUES (?, ?, ?, ?, ?, ?)",
            (row.job_id, row.episode_id, row.current_stage, row.status, seq, seq),
        )
        return row

    def _require_job(self, job_id: str) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise StateStoreError("job-missing", f"no job row for {job_id}")

    def get_job_snapshot(self, job_id: Identifier) -> JobSnapshot:
        self._require_job(job_id)
        job_row = self._connection.execute(
            "SELECT job_id, episode_id, current_stage, status, created_at_seq,"
            " updated_at_seq FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if job_row is None:
            raise StateStoreError("job-missing", f"no job row for {job_id}")
        job = JobRow(
            job_id=text_column(job_row[0]),
            episode_id=text_column(job_row[1]),
            current_stage=text_column(job_row[2]),
            status=cast("JobStatus", text_column(job_row[3])),
            created_at_seq=int_column(job_row[4]),
            updated_at_seq=int_column(job_row[5]),
        )
        runs = self._connection.execute(
            f"SELECT {STAGE_RUN_COLUMNS} FROM stage_runs WHERE job_id = ? ORDER BY rowid",  # noqa: S608 (constant columns)
            (job_id,),
        ).fetchall()
        return JobSnapshot(job=job, stage_runs=tuple(stage_run_from_row(r) for r in runs))


__all__ = [
    "LATEST_VERSION",
    "MigrationError",
    "StateStore",
    "StateStoreError",
]
