"""episode_status run-visibility derivation (工程2P).

Read-only assembly of the GET /episodes/{id} payload's run and clock
fields, scoped honestly to the CURRENT run and plan version. Reads only
existing runtime records (StateStore snapshot, rebuild-request jsonl, a
BOUNDED tail of runner.log, intake record, review-proposal records);
absent evidence is ``None`` / ``False`` / an omitted per-row field —
unmeasured is never fabricated. Runtime State, not an artifact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.episode_cockpit.models import IntakeRecordV1, RebuildRequestEntry
from services.episode_cockpit.preview_binding import published_target_version
from services.episode_cockpit.review_proposals import (
    latest_unconsumed_set,
    load_consumed_proposals,
    load_proposal_sets,
)
from services.episode_cockpit.self_check import latest_self_check
from services.review_command.store import ReviewCommitError, load_head

if TYPE_CHECKING:
    from services.job_runner.state_models import JobSnapshot, StageRunRow

# Mirrors of episode_files/episode_ops constants — kept inline so this
# module imports neither mixin (they import this module's consumer).
RUNNER_LOG_NAME: Final = "runner.log"
REBUILD_LOG_NAME: Final = "rebuild-requests.jsonl"
_REVIEW_EVENTS_RELATIVE: Final = ("review", "events.jsonl")
_REVIEW_STORE_RELATIVE: Final = ("review", "store")
TAIL_BYTES: Final = 65536
MAX_REBUILD_LINES: Final = 200


def tail_events(log_path: Path, *, max_bytes: int = TAIL_BYTES) -> list[dict[str, object]]:
    """Bounded TAIL parse of one runner log (never a whole-file scan)."""

    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as stream:
            if size > max_bytes:
                stream.seek(size - max_bytes)
            payload = stream.read()
    except OSError:
        return []
    events: list[dict[str, object]] = []
    for line in payload.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def load_rebuild_entries(episode_dir: Path) -> list[RebuildRequestEntry]:
    """The recent rebuild-request chain, pre-spawn reservations included."""

    try:
        lines = (episode_dir / REBUILD_LOG_NAME).read_bytes().splitlines()
    except OSError:
        return []
    entries: list[RebuildRequestEntry] = []
    for line in lines[-MAX_REBUILD_LINES:]:
        try:
            entries.append(RebuildRequestEntry.model_validate_json(line))
        except ValidationError:
            continue
    return entries


def derive_current_run(
    entries: list[RebuildRequestEntry], runner_log: Path
) -> tuple[str | None, str | None]:
    """(current_run, current_target_version) — never an OLD run.

    The latest SPAWNED rebuild entry owns the current run; a newer
    unspawned entry (reservation or intent) means nothing is running for
    the latest request — current_run is None and 予約済み stays
    derivable from ``rebuild_requests``. Only with NO rebuild records at
    all does the initial chain run count (last ``runner_started`` in the
    bounded log tail); its target version comes from the latest COMPLETE
    ``preview_published`` record only — an old-format or incomplete
    record keeps the version honestly unknown (None).
    """

    current: str | None = None
    target: str | None = None
    for entry in entries:
        if entry.spawned and entry.run_id is not None:
            current = entry.run_id
            target = entry.target_version
        else:
            current = None
            target = entry.target_version
    if current is None and not entries:
        for event in reversed(tail_events(runner_log)):
            if event.get("event") == "runner_started" and event.get("run_id"):
                return str(event["run_id"]), published_target_version(runner_log)
    return current, target


def pending_rebuild(entries: list[RebuildRequestEntry]) -> dict[str, object] | None:
    """The unspawned reservation when it is the LATEST entry (起動待ち)."""

    if not entries or entries[-1].spawned:
        return None
    latest = entries[-1]
    return {
        "sequence": latest.sequence,
        "stage_hint": latest.stage_hint,
        "marker": latest.marker,
        "run_id": latest.run_id,
        "target_version": latest.target_version,
    }


def last_worker_report(runner_log: Path, run_id: str | None) -> tuple[str, str] | None:
    """The run-scoped last 動作報告 (ts, kind) — NOT a liveness claim.

    Terminal/failure events count as reports. Old lines without a run_id
    are outside this run's scope — falls back to None (応答不明).
    """

    if run_id is None:
        return None
    for event in reversed(tail_events(runner_log)):
        if event.get("run_id") == run_id and event.get("ts"):
            return str(event["ts"]), str(event.get("event"))
    return None


def unreviewed_proposal_set(episode_dir: Path) -> bool:
    """True iff an UNCONSUMED proposal set answers the CURRENT plan head
    (mirrors the apply route's ``_require_fresh_base`` scoping)."""

    sets = load_proposal_sets(episode_dir)
    if not sets:
        return False
    latest = latest_unconsumed_set(sets, load_consumed_proposals(episode_dir))
    if latest is None:
        return False
    try:
        head_version: int | None = load_head(
            episode_dir.joinpath(*_REVIEW_EVENTS_RELATIVE),
            episode_dir.joinpath(*_REVIEW_STORE_RELATIVE),
        ).version
    except (ReviewCommitError, OSError):
        head_version = None
    if latest.base_plan_version is None:
        return head_version in (None, 1)
    try:
        return head_version == int(latest.base_plan_version[1:])
    except ValueError:
        return False


def intake_created_at(episode_dir: Path) -> str | None:
    try:
        return IntakeRecordV1.model_validate_json(
            (episode_dir / "intake.json").read_bytes()
        ).created_at
    except (OSError, ValidationError):
        return None


def intake_applied_style(episode_dir: Path) -> dict[str, object] | None:
    """The 工程3 style pin recorded at create time (None = unpinned)."""

    try:
        record = IntakeRecordV1.model_validate_json(
            (episode_dir / "intake.json").read_bytes()
        )
    except (OSError, ValidationError):
        return None
    if record.channel is None:
        return None
    return {"channel": record.channel, "version": record.style_version}


def stage_row(run: StageRunRow) -> dict[str, object]:
    row: dict[str, object] = {
        "stage_name": run.stage_name,
        "status": run.status,
        "retry_count": run.retry_count,
        "last_error_code": run.last_error_code,
    }
    if run.run_id is not None:
        row["run_id"] = run.run_id
    if run.first_started_at is not None:
        row["first_started_at"] = run.first_started_at
    if run.last_transition_at is not None:
        row["last_transition_at"] = run.last_transition_at
    if run.first_output_arrived_at is not None:
        row["first_output_arrived_at"] = run.first_output_arrived_at
    return row


def build_status_payload(snapshot: JobSnapshot, episode_dir: Path) -> dict[str, object]:
    runner_log = episode_dir / RUNNER_LOG_NAME
    entries = load_rebuild_entries(episode_dir)
    current_run, current_target = derive_current_run(entries, runner_log)
    report = last_worker_report(runner_log, current_run)
    job = snapshot.job
    payload: dict[str, object] = {
        "episode_id": job.episode_id,
        "job_id": job.job_id,
        "status": job.status,
        "current_stage": job.current_stage,
        "created_at_seq": job.created_at_seq,
        "updated_at_seq": job.updated_at_seq,
        "stage_runs": [stage_row(run) for run in snapshot.stage_runs],
        "current_run": current_run,
        "current_target_version": current_target,
        "pending_rebuild": pending_rebuild(entries),
        "rebuild_requests": [entry.model_dump(mode="json") for entry in entries],
        "last_worker_report_at": report[0] if report else None,
        "last_worker_report_event": report[1] if report else None,
        "unreviewed_proposal_set": unreviewed_proposal_set(episode_dir),
    }
    retry = next(
        (
            run.retry_count for run in snapshot.stage_runs
            if current_run is not None and run.run_id == current_run
            and run.status == "running" and run.retry_count > 0
        ),
        None,
    )
    if retry is not None:
        payload["current_run_retry_count"] = retry
    created_at = intake_created_at(episode_dir)
    if created_at is not None:
        payload["intake_created_at"] = created_at
    payload["applied_style"] = intake_applied_style(episode_dir)
    latest = latest_self_check(episode_dir)
    payload["self_check"] = latest.model_dump(mode="json") if latest is not None else None
    arrived = next(
        (
            run.first_output_arrived_at for run in snapshot.stage_runs
            if current_run is not None and run.run_id == current_run
            and run.stage_name == "preview" and run.first_output_arrived_at
        ),
        None,
    )
    if arrived is not None:
        payload["preview_first_arrived_at"] = arrived
    return payload


__all__ = [
    "build_status_payload",
    "derive_current_run",
    "intake_applied_style",
    "last_worker_report",
    "load_rebuild_entries",
    "pending_rebuild",
    "stage_row",
    "tail_events",
    "unreviewed_proposal_set",
]
