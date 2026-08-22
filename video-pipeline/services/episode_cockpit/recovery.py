"""Restart recovery: rebuild the operator view from persisted state only.

The cockpit keeps no in-memory state, so recovery is a pure read: job
rows and stage runs come from one read-only SQLite connection over the
StateStore file, and acceptances/pending reviews come from the episode
workspace files (append-only approval ledger, review chat log) — an
acceptance is a file/ledger fact, so it survives any restart. Raw row
values are parsed exactly once through the strict view models (a
corrupt row fails closed as a typed error), and an unreadable store
surfaces as ``RecoveryError`` carrying the operator recovery hint
instead of a raw sqlite traceback.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from services.approvals.store import OperationRecordError, OperationRecordStore
from services.contracts.primitives import Identifier, StrictModel
from services.episode_cockpit.episode_files import CHAT_LOG_NAME
from services.episode_cockpit.side_desks import APPROVALS_RELATIVE

# Runtime imports (NOT TYPE_CHECKING): pydantic resolves JobStatus and
# StageRunStatus while building the StrictModel fields below, at class time.
from services.job_runner.state_models import JobStatus, StageRunStatus  # noqa: TC001

STATE_UNREADABLE_HINT = (
    "ジョブ状態DBを読み込めません。brief・レビュー記録・承認台帳は"
    " episodes フォルダ側に無傷で残っています。"
    "state.dbのバックアップを復元するか、対象エピソードを再intakeしてください。"
)

APPROVALS_UNREADABLE_HINT = (
    "承認台帳(records.jsonl)が壊れています。当該エピソードの承認は信用できないため、"
    "台帳を修復するまで再承認してください。"
)


class RecoveryError(Exception):
    """Typed unreadable-state failure with the operator recovery hint."""

    def __init__(self, code: str, detail: str, *, hint: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.hint = hint


class StageProgress(StrictModel):
    """One stage run's position in the resumed view."""

    stage_name: Identifier
    status: StageRunStatus
    retry_count: int = Field(ge=0, strict=True)


class ApprovalFact(StrictModel):
    """One surviving acceptance/rejection fact from the approval ledger."""

    purpose: Identifier
    decision: Literal["approve", "reject"]
    record_id: Identifier


class RecoveredEpisode(StrictModel):
    """One episode's rebuilt operator view."""

    episode_id: Identifier
    job_id: Identifier
    status: JobStatus
    current_stage: Identifier
    stage_runs: tuple[StageProgress, ...]
    approvals: tuple[ApprovalFact, ...]
    review_messages: int = Field(ge=0, strict=True)


class RecoveryView(StrictModel):
    """The full operator view after a restart."""

    schema_version: Literal["cockpit-recovery-v1"] = "cockpit-recovery-v1"
    episodes: tuple[RecoveredEpisode, ...]


def resume_state(state_store_path: Path, episodes_root: Path) -> RecoveryView:
    """Rebuild the operator view purely from persisted StateStore + files."""

    if not state_store_path.is_file():
        return RecoveryView(episodes=())
    uri = f"{state_store_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        episodes = tuple(
            _recover_episode(connection, episodes_root, row)
            for row in _read_job_rows(connection)
        )
    except sqlite3.DatabaseError as error:
        raise RecoveryError(
            "state-store-unreadable", str(error), hint=STATE_UNREADABLE_HINT
        ) from error
    finally:
        connection.close()
    return RecoveryView(episodes=episodes)


def _read_job_rows(connection: sqlite3.Connection) -> list[Sequence[object]]:
    rows = connection.execute(
        "SELECT job_id, episode_id, current_stage, status FROM jobs ORDER BY created_at_seq"
    ).fetchall()
    return [tuple(row) for row in rows]


def _recover_episode(
    connection: sqlite3.Connection, episodes_root: Path, job_row: Sequence[object]
) -> RecoveredEpisode:
    job_id, episode_id, current_stage, status = job_row
    episode_dir = episodes_root / str(episode_id)
    stage_rows = connection.execute(
        "SELECT stage_name, status, retry_count FROM stage_runs"
        " WHERE job_id = ? ORDER BY rowid",
        (job_id,),
    ).fetchall()
    try:
        return RecoveredEpisode.model_validate(
            {
                "episode_id": episode_id,
                "job_id": job_id,
                "status": status,
                "current_stage": current_stage,
                "stage_runs": tuple(
                    {
                        "stage_name": row[0],
                        "status": row[1],
                        "retry_count": row[2],
                    }
                    for row in stage_rows
                ),
                "approvals": tuple(
                    fact.model_dump() for fact in _approval_facts(episode_dir)
                ),
                "review_messages": _count_lines(episode_dir / CHAT_LOG_NAME),
            }
        )
    except ValidationError as error:
        raise RecoveryError(
            "state-store-unreadable", str(error), hint=STATE_UNREADABLE_HINT
        ) from error


def _approval_facts(episode_dir: Path) -> tuple[ApprovalFact, ...]:
    records_path = episode_dir.joinpath(*APPROVALS_RELATIVE)
    if not records_path.is_file():
        return ()
    store = OperationRecordStore(records_path)
    try:
        latest = store.latest_records()
    except OperationRecordError as error:
        raise RecoveryError(
            "approval-ledger-unreadable", str(error), hint=APPROVALS_UNREADABLE_HINT
        ) from error
    facts = [
        ApprovalFact(
            purpose=record.purpose, decision=record.decision, record_id=record.record_id
        )
        for record in latest.values()
    ]
    return tuple(sorted(facts, key=lambda fact: fact.record_id))


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_bytes().splitlines() if line.strip())


__all__ = [
    "ApprovalFact",
    "RecoveredEpisode",
    "RecoveryError",
    "RecoveryView",
    "StageProgress",
    "resume_state",
]
