"""Cockpit job-state mirroring for the episode runner (task 7).

The real chain drives its OWN StateStore (job ``job-real-episode-run``
under ``<episode-root>/run/state.sqlite3``); the cockpit's job row
(job id = episode id) lives in the cockpit StateStore. This module is
the bridge: it observes the chain's committed statuses and replays the
exact same MAIN_PATH edges into the cockpit store through
``apply_transition`` CAS — the ``real_chain._advance`` pattern — so the
StateStore remains the ONLY job-state authority and the cockpit's 2s
poll sees truthful stage_runs progress. One ``succeeded`` stage-run row
per mirrored status (stage names from the cockpit PIPELINE_STAGES
vocabulary) plus a ``running`` frontier row for the stage in flight; a
hard runner death leaves that frontier row honestly stale.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final

from services.cli.real_chain import JOB_ID as CHAIN_JOB_ID
from services.job_runner.cas import apply_transition, current_job_state
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_models import JobStatus, StageRunRow
from services.job_runner.state_store import StateStore
from services.job_runner.transitions import MAIN_PATH

if TYPE_CHECKING:
    from services.contracts.primitives import Sha256

RUNNER_VERSION: Final = "cockpit-episode-runner-v1"
WATCH_POLL_SECONDS: Final = 1.0

# Cockpit stage vocabulary (PIPELINE_STAGES names) per mirrored job status;
# "intake" (the runner pick-up) is recorded by the orchestrator pre-chain.
STAGE_OF_STATUS: Final[dict[JobStatus, str]] = {
    "INGESTED": "ingest",
    "NORMALIZED": "normalize",
    "ANALYZED": "analyze",
    "PLAN_PROPOSED": "selection",
    "PLAN_COMMITTED": "plan",
    "PREVIEW_READY": "preview",
}

_LOG_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class RunContext:
    """Wiring shared by every state-mirror operation of one runner run."""

    state_store_path: Path
    job_id: str
    run_id: str
    stop: str
    log: BinaryIO


def log_event(stream: BinaryIO, event: str, **fields: object) -> None:
    payload = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
    with _LOG_LOCK:
        stream.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        stream.flush()


def record_stage(  # noqa: PLR0913 (store + ctx + the StageRunRow field contract)
    store: StateStore,
    ctx: RunContext,
    stage: str,
    status: str,
    *,
    error_code: str | None = None,
    adopted: str | None = None,
    now: str | None = None,
) -> None:
    """Record one stage transition with caller-supplied (or current) wall clock.

    The clock fields are SEPARATED (2P): ``first_started_at`` freezes the
    first entry into ``running``; ``first_output_arrived_at`` is stamped
    ONLY on ``succeeded`` and the store never overwrites it once set;
    ``last_transition_at`` moves only when the row's meaning changes.
    """

    stamp = datetime.now(UTC).isoformat() if now is None else now
    store.record_stage_run(
        StageRunRow(
            job_id=ctx.job_id,
            stage_name=stage,
            input_artifact_hashes=(),
            adopted_artifact_hash=adopted,
            status=status,  # type: ignore[arg-type] (stage-run statuses)
            idempotency_key=f"{RUNNER_VERSION}:{ctx.run_id}:{stage}",
            last_error_code=error_code,
            run_id=ctx.run_id,
            first_started_at=stamp if status == "running" else None,
            first_output_arrived_at=stamp if status == "succeeded" else None,
            last_transition_at=stamp,
        )
    )


def block_stage(
    store: StateStore, ctx: RunContext, stage: str, code: str, *, now: str | None = None
) -> None:
    """Record one blocked stage run through the existing StateStore semantics."""

    record_stage(store, ctx, stage, "failed_blocked", error_code=code, now=now)


def block_running_frontier(
    store: StateStore, ctx: RunContext, code: str, *, now: str | None = None
) -> None:
    """Mark this run's stale ``running`` rows failed_blocked (the honest halt)."""

    prefix = f"{RUNNER_VERSION}:{ctx.run_id}:"
    stamp = datetime.now(UTC).isoformat() if now is None else now
    for row in store.get_job_snapshot(ctx.job_id).stage_runs:
        if row.idempotency_key.startswith(prefix) and row.status == "running":
            store.set_stage_status(
                job_id=ctx.job_id,
                idempotency_key=row.idempotency_key,
                status="failed_blocked",
                last_error_code=code,
                last_transition_at=stamp,
            )


def mirror_upto(
    store: StateStore,
    ctx: RunContext,
    observed: JobStatus,
    observed_hash: Sha256,
) -> None:
    """Advance the cockpit job to ``observed`` via apply_transition CAS.

    Walks the exact MAIN_PATH edges the chain itself took (re-read
    current, CAS with it as the expectation). Statuses the chain skipped
    between two observations reuse the latest observed hash — the status
    path is the truthful part. After catching up (and while the chain is
    still short of the stop target) the next stage is recorded running.
    """

    while True:
        current = current_job_state(store, ctx.job_id)
        if MAIN_PATH.index(observed) <= MAIN_PATH.index(current.status):
            break
        nxt = MAIN_PATH[MAIN_PATH.index(current.status) + 1]
        stage = STAGE_OF_STATUS[nxt]
        apply_transition(
            store,
            ctx.job_id,
            expected_status=current.status,
            expected_parent_hash=current.adopted_artifact_hash,
            new_status=nxt,
            new_artifact_hash=observed_hash,
        )
        record_stage(store, ctx, stage, "succeeded", adopted=observed_hash)
        log_event(
            ctx.log, "stage", run_id=ctx.run_id, stage=stage, status="succeeded",
            job_status=nxt,
        )
    if observed != ctx.stop:
        ahead = STAGE_OF_STATUS.get(MAIN_PATH[MAIN_PATH.index(observed) + 1])
        if ahead is not None:
            record_stage(store, ctx, ahead, "running")


def read_chain(chain_state: Path) -> tuple[JobStatus, Sha256] | None:
    """Latest (status, adopted hash) of the chain's own job, or None."""

    if not chain_state.is_file():
        return None
    try:
        with StateStore.open(chain_state) as chain:
            snapshot = current_job_state(chain, CHAIN_JOB_ID)
    except (StateStoreError, sqlite3.Error):
        return None
    if snapshot.status == "CREATED" or snapshot.adopted_artifact_hash is None:
        return None
    return snapshot.status, snapshot.adopted_artifact_hash


def watch_chain(
    stop_event: threading.Event, chain_state: Path, ctx: RunContext
) -> None:
    """Daemon mirror: poll the chain's WAL db and mirror transitions live.

    Reads race no writer (WAL); every mirrored write is CAS-guarded, so
    a late observation can only be refused as superseded, never corrupt.
    """

    try:
        store = StateStore.open(ctx.state_store_path)
    except (StateStoreError, sqlite3.Error) as error:
        log_event(
            ctx.log, "watcher_stopped", run_id=ctx.run_id, reason=f"open-failed: {error}"
        )
        return
    try:
        while not stop_event.wait(WATCH_POLL_SECONDS):
            observed = read_chain(chain_state)
            if observed is None:
                continue
            try:
                mirror_upto(store, ctx, observed[0], observed[1])
            except (StateStoreError, sqlite3.Error) as error:
                log_event(
                    ctx.log, "watcher_error", run_id=ctx.run_id, reason=str(error)
                )
    finally:
        store.close()


__all__ = [
    "RUNNER_VERSION",
    "STAGE_OF_STATUS",
    "RunContext",
    "block_running_frontier",
    "block_stage",
    "log_event",
    "mirror_upto",
    "read_chain",
    "record_stage",
    "watch_chain",
]
