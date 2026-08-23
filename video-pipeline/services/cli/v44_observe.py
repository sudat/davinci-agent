"""Observation tooling for Gate V44-1 — cockpit vertical slice.

Collects the honest wall-clock evidence an operator needs to judge the
V44-1 flow: intake→PREVIEW_READY timings, NL corrections, rebuild
metrics, operator sign-off, and the anti-fabrication guards that refuse
to certify seeded state.

Anti-fabrication (seed-refusal)
--------------------------------
A seeded e2e harness (``cockpit/tests/e2e/acceptance.spec.ts``
``SEED_PIPELINE_STATE``) writes job rows whose idempotency keys are
``"<stage>-run-1"`` and no ``runner.log``. A real run appends stage
rows whose keys are ``"cockpit-episode-runner-v1:<run_id>:<stage>"``
(``RUNNER_VERSION`` in ``services.cli.episode_runner_state``) and the
detached runner holds ``runner.log`` for its whole lifetime. The
observer refuses to certify whenever ANY of the three signals is
missing: ``runner.log``, ≥1 stage row, ≥1 runner-lineage stage row.
Exit ``seeded-state-refused`` with no ``observation.json``.

TTFRP derivation
----------------
The job-runner ``jobs``/``stage_runs`` tables carry only logical
``created_at_seq``/``updated_at_seq`` counters (``state_models``: never
a wall clock). The honest wall-clock pair is therefore:
``intake.json:created_at → previews/preview.mp4 mtime``. Either side
missing ⇒ ``ttfrp_seconds = null`` (unknown, never fabricated).

Correction semantics
--------------------
``applied`` is episode-level evidence: True iff the episode has ≥1
non-deferred ``AppliedCommand`` (the cockpit records NL text in
``review-chat.jsonl`` and the structured command without text in
``applied-commands.jsonl`` — per-correction correlation is not persisted,
so the bool is uniform across the batch and documented as such).
``rebuild_wall_clock_seconds`` is the latest rebuild metric's wall time
(same caveat). ``internal_path_leak`` scans chat texts for
``"jobs/"``/``"artifacts/"``/``".json"`` — the operator path the
V44-1 runbook forbids requiring.

Injectability
-------------
The observer's core is two pure functions (``collect_observation_inputs``
is disk/DB I/O; ``build_observation_record`` is pure). Tests inject a
tmp workspace; no live server is needed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from services.cli.episode_runner_state import RUNNER_VERSION
from services.foundation_io import atomic_write, canonical_model_bytes
from services.metrics.v44_gate_state import (
    ObservationCorrectionV1,
    ObservationRebuildV1,
    ObservationStageRunV1,
    ObservationStageTimelineV1,
    V1ObservationRecord,
)

OPERATOR_NOTES_NAME = "operator-notes.jsonl"
CHAT_LOG_NAME = "review-chat.jsonl"
APPLIED_COMMANDS_NAME = "applied-commands.jsonl"
REBUILD_METRICS_NAME = "rebuild-metrics.jsonl"
INTAKE_NAME = "intake.json"
PREVIEW_RELATIVE: tuple[str, ...] = ("previews", "preview.mp4")
RUNNER_LOG_NAME = "runner.log"


class V44ObserveError(Exception):
    """Typed observer refusal — ``code`` maps to an operator-actionable reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class StageRunForObservation:
    stage_name: str
    status: str
    retry_count: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RebuildMetricForObservation:
    sequence: int
    applied_command: str
    stages: tuple[str, ...]
    rebuild_wall_clock_seconds: float
    unrelated_stages_skipped: tuple[str, ...]
    at: str
    interpretation_ms: float | None = None


@dataclass(frozen=True, slots=True)
class EpisodeObservationInputs:
    """Snapshot of everything the pure builder needs — no hidden I/O."""

    episode_id: str
    job_status: str
    current_stage: str
    stage_runs: tuple[StageRunForObservation, ...]
    runner_log_exists: bool
    intake_created_at: str | None
    preview_mtime_epoch: float | None
    chat_entries: tuple[tuple[str, float | None], ...]
    has_applied_command: bool
    applied_count: int
    rebuild_metrics: tuple[RebuildMetricForObservation, ...]
    operator_note: str | None


# ---------------------------------------------------------------------------
# Readers (disk + read-only DB) — tolerant where the absence is honest state
# ---------------------------------------------------------------------------


def _read_job_and_stage_runs(
    episode_id: str,
    state_store_path: Path,
) -> tuple[str, str, tuple[StageRunForObservation, ...]]:
    if not state_store_path.is_file():
        raise V44ObserveError(
            "episode-not-found",
            f"state store not found at {state_store_path}",
        )
    uri = f"{state_store_path.resolve().as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise V44ObserveError("state-store-unreadable", str(error)) from error
    try:
        try:
            row = connection.execute(
                "SELECT status, current_stage FROM jobs WHERE job_id = ?",
                (episode_id,),
            ).fetchone()
        except sqlite3.Error as error:
            raise V44ObserveError("state-store-unreadable", str(error)) from error
        if row is None:
            raise V44ObserveError(
                "episode-not-found",
                f"no job row for episode {episode_id} in {state_store_path}",
            )
        job_status = str(row[0])
        current_stage = str(row[1])
        try:
            raw_runs = connection.execute(
                "SELECT stage_name, status, retry_count, idempotency_key"
                " FROM stage_runs WHERE job_id = ? ORDER BY rowid",
                (episode_id,),
            ).fetchall()
        except sqlite3.Error as error:
            raise V44ObserveError("state-store-unreadable", str(error)) from error
        runs = [
            StageRunForObservation(
                stage_name=str(raw[0]),
                status=str(raw[1]),
                retry_count=int(raw[2]),
                idempotency_key=str(raw[3]),
            )
            for raw in raw_runs
        ]
        return job_status, current_stage, tuple(runs)
    finally:
        connection.close()


def _read_intake_created_at(episode_dir: Path) -> str | None:
    path = episode_dir / INTAKE_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    value = data.get("created_at")
    return str(value) if isinstance(value, str) and len(value) > 0 else None


def _read_preview_mtime_epoch(episode_dir: Path) -> float | None:
    preview = episode_dir.joinpath(*PREVIEW_RELATIVE)
    if not preview.is_file():
        return None
    try:
        return float(preview.stat().st_mtime)
    except OSError:
        return None


def _read_chat_entries(episode_dir: Path) -> tuple[tuple[str, float | None], ...]:
    path = episode_dir / CHAT_LOG_NAME
    if not path.is_file():
        return ()
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise V44ObserveError("episode-file-unreadable", str(error)) from error
    entries: list[tuple[str, float | None]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError) as error:
            raise V44ObserveError(
                "chat-log-corrupt",
                f"line {lineno} in {path} is not JSON: {error}",
            ) from error
        if not isinstance(obj, dict):
            raise V44ObserveError(
                "chat-log-corrupt",
                f"line {lineno} in {path} is not an object",
            )
        text = obj.get("text")
        if not isinstance(text, str) or len(text) == 0:
            raise V44ObserveError(
                "chat-log-corrupt",
                f"line {lineno} in {path} has no non-empty text",
            )
        at_raw = obj.get("at_seconds")
        at_seconds: float | None = None
        if at_raw is not None:
            try:
                at_seconds = float(at_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError) as error:
                raise V44ObserveError(
                    "chat-log-corrupt",
                    f"line {lineno} in {path} has invalid at_seconds",
                ) from error
        entries.append((text, at_seconds))
    return tuple(entries)


def _read_applied_state(episode_dir: Path) -> tuple[int, bool]:
    path = episode_dir / APPLIED_COMMANDS_NAME
    if not path.is_file():
        return 0, False
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise V44ObserveError("episode-file-unreadable", str(error)) from error
    count = 0
    has_applied = False
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError) as error:
            raise V44ObserveError(
                "applied-commands-corrupt",
                f"line {lineno} in {path}: {error}",
            ) from error
        count += 1
        if isinstance(obj, dict) and obj.get("deferred") is not True:
            has_applied = True
    return count, has_applied


def _read_rebuild_metrics(episode_dir: Path) -> tuple[RebuildMetricForObservation, ...]:
    path = episode_dir / REBUILD_METRICS_NAME
    if not path.is_file():
        return ()
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise V44ObserveError("episode-file-unreadable", str(error)) from error
    metrics: list[RebuildMetricForObservation] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError) as error:
            raise V44ObserveError(
                "rebuild-metrics-corrupt",
                f"line {lineno} in {path}: {error}",
            ) from error
        if not isinstance(obj, dict):
            raise V44ObserveError(
                "rebuild-metrics-corrupt",
                f"line {lineno} in {path} is not an object",
            )
        try:
            sequence = int(obj["sequence"])
            applied_command = str(obj["applied_command"])
            raw_stages = obj.get("stages", [])
            stages = tuple(str(s) for s in raw_stages) if isinstance(raw_stages, list) else ()
            wall = float(obj["rebuild_wall_clock_seconds"])
            raw_skipped = obj.get("unrelated_stages_skipped", [])
            skipped = (
                tuple(str(s) for s in raw_skipped) if isinstance(raw_skipped, list) else ()
            )
            at = str(obj["at"])
            interp_raw = obj.get("interpretation_ms")
            interp: float | None = None if interp_raw is None else float(interp_raw)  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError) as error:
            raise V44ObserveError(
                "rebuild-metrics-corrupt",
                f"line {lineno} in {path} missing/invalid fields: {error}",
            ) from error
        metrics.append(
            RebuildMetricForObservation(
                sequence=sequence,
                applied_command=applied_command,
                stages=stages,
                rebuild_wall_clock_seconds=wall,
                unrelated_stages_skipped=skipped,
                at=at,
                interpretation_ms=interp,
            )
        )
    return tuple(metrics)


def _read_operator_note(episode_dir: Path) -> str | None:
    path = episode_dir / OPERATOR_NOTES_NAME
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise V44ObserveError("episode-file-unreadable", str(error)) from error
    last_note: str | None = None
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, json.JSONDecodeError) as error:
            raise V44ObserveError(
                "operator-notes-corrupt",
                f"line {lineno} in {path}: {error}",
            ) from error
        if not isinstance(obj, dict):
            continue
        note = obj.get("note")
        if isinstance(note, str) and len(note) > 0:
            last_note = note
    return last_note


def collect_observation_inputs(
    episode_id: str,
    *,
    episodes_root: Path,
    state_store_path: Path,
) -> EpisodeObservationInputs:
    """Read-only snapshot of one cockpit episode — no writes, no polling.

    ``state_store_path`` is opened read-only (``mode=ro`` URI, the same
    discipline ``episode_ops._list_job_rows`` uses while the server is
    live). Every episode file is re-derived from disk.
    """

    episode_dir = episodes_root / episode_id
    if not episode_dir.is_dir():
        raise V44ObserveError(
            "episode-dir-not-found",
            f"no episode dir at {episode_dir}",
        )
    job_status, current_stage, stage_runs = _read_job_and_stage_runs(
        episode_id,
        state_store_path,
    )
    return EpisodeObservationInputs(
        episode_id=episode_id,
        job_status=job_status,
        current_stage=current_stage,
        stage_runs=stage_runs,
        runner_log_exists=(episode_dir / RUNNER_LOG_NAME).is_file(),
        intake_created_at=_read_intake_created_at(episode_dir),
        preview_mtime_epoch=_read_preview_mtime_epoch(episode_dir),
        chat_entries=_read_chat_entries(episode_dir),
        has_applied_command=_read_applied_state(episode_dir)[1],
        applied_count=_read_applied_state(episode_dir)[0],
        rebuild_metrics=_read_rebuild_metrics(episode_dir),
        operator_note=_read_operator_note(episode_dir),
    )


def _compute_ttfrp_seconds(
    intake_created_at: str | None,
    preview_mtime_epoch: float | None,
) -> float | None:
    if intake_created_at is None or preview_mtime_epoch is None:
        return None
    try:
        created_epoch = datetime.fromisoformat(intake_created_at).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None
    delta = preview_mtime_epoch - created_epoch
    if delta < 0:
        return None
    return round(float(delta), 3)


def build_observation_record(
    inputs: EpisodeObservationInputs,
    *,
    observed_at: str | None = None,
) -> V1ObservationRecord:
    """Pure builder — validates and refuses seeded state.

    Raises ``V44ObserveError("seeded-state-refused", ...)`` when the
    workspace lacks real-run evidence. Writes no file.
    """

    runner_lineage_rows = sum(
        1 for run in inputs.stage_runs if run.idempotency_key.startswith(f"{RUNNER_VERSION}:")
    )
    if not (
        inputs.runner_log_exists
        and len(inputs.stage_runs) > 0
        and runner_lineage_rows > 0
    ):
        raise V44ObserveError(
            "seeded-state-refused",
            (
                "real-run evidence missing:"
                f" runner_log={inputs.runner_log_exists}"
                f" stage_rows={len(inputs.stage_runs)}"
                f" runner_lineage_rows={runner_lineage_rows};"
                " seeded state (cockpit acceptance harness SEED_PIPELINE_STATE"
                " or empty workspace) cannot be certified — do not simulate"
                " this gate; run the real Cockpit flow"
            ),
        )

    ttfrp = _compute_ttfrp_seconds(inputs.intake_created_at, inputs.preview_mtime_epoch)
    leak = any(
        ("jobs/" in text or "artifacts/" in text or ".json" in text)
        for text, _ in inputs.chat_entries
    )
    latest_wall: float | None = (
        inputs.rebuild_metrics[-1].rebuild_wall_clock_seconds
        if len(inputs.rebuild_metrics) > 0
        else None
    )
    corrections = tuple(
        ObservationCorrectionV1(
            text=text,
            at_seconds=at_seconds,
            applied=inputs.has_applied_command,
            rebuild_wall_clock_seconds=latest_wall,
        )
        for text, at_seconds in inputs.chat_entries
    )
    rebuild_records = tuple(
        ObservationRebuildV1(
            sequence=metric.sequence,
            applied_command=metric.applied_command,
            stages=metric.stages,
            rebuild_wall_clock_seconds=metric.rebuild_wall_clock_seconds,
            unrelated_stages_skipped=metric.unrelated_stages_skipped,
            at=metric.at,
            interpretation_ms=metric.interpretation_ms,
        )
        for metric in inputs.rebuild_metrics
    )
    stage_timeline = ObservationStageTimelineV1(
        job_status=inputs.job_status,  # type: ignore[arg-type]
        current_stage=inputs.current_stage,  # type: ignore[arg-type]
        runs=tuple(
            ObservationStageRunV1(
                stage_name=run.stage_name,  # type: ignore[arg-type]
                status=run.status,  # type: ignore[arg-type]
                retry_count=run.retry_count,
                idempotency_key=run.idempotency_key,
            )
            for run in inputs.stage_runs
        ),
    )
    at_value = observed_at if observed_at is not None else datetime.now(tz=UTC).isoformat()
    try:
        return V1ObservationRecord(
            episode_id=inputs.episode_id,  # type: ignore[arg-type]
            stage_timeline=stage_timeline,
            ttfrp_seconds=ttfrp,
            corrections=corrections,
            rebuild_records=rebuild_records,
            operator_note=inputs.operator_note,
            internal_path_leak=leak,
            observed_at=at_value,
        )
    except ValidationError as error:
        raise V44ObserveError("observation-input-invalid", str(error)) from error


def write_observation(out_dir: Path, record: V1ObservationRecord) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "observation.json"
    atomic_write(out_path, canonical_model_bytes(record))
    return out_path


# ---------------------------------------------------------------------------
# Operator-note recording (sign-off path — small, additive)
# ---------------------------------------------------------------------------


def record_operator_note(
    *,
    episode_id: str,
    note: str,
    episodes_root: Path,
) -> Path:
    """Append one operator note to the episode dir (communication file)."""

    cleaned = note.strip()
    if len(cleaned) == 0:
        raise V44ObserveError("note-empty", "operator note must be non-empty")
    episode_dir = episodes_root / episode_id
    if not episode_dir.is_dir():
        raise V44ObserveError(
            "episode-dir-not-found",
            f"no episode dir at {episode_dir}",
        )
    log_path = episode_dir / OPERATOR_NOTES_NAME
    payload = {
        "schema_version": "v44-operator-note-v1",
        "episode_id": episode_id,
        "note": cleaned,
        "at": datetime.now(tz=UTC).isoformat(),
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as stream:
        stream.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
    return log_path


__all__ = [
    "EpisodeObservationInputs",
    "RebuildMetricForObservation",
    "StageRunForObservation",
    "V44ObserveError",
    "build_observation_record",
    "collect_observation_inputs",
    "record_operator_note",
    "write_observation",
]
