# allow: SIZE_OK — plan-pinned T15 observer verification drills (seed-refusal,
# real-run, leak, note) in one file; same precedent as episode_runner_rebuild.py.
"""Gate V44-1 observer drills: seeded refusal + real-run validation (T15)."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.cli.episode_runner_state import RUNNER_VERSION
from services.cli.v44_observe import (
    EpisodeObservationInputs,
    RebuildMetricForObservation,
    StageRunForObservation,
    V44ObserveError,
    build_observation_record,
)
from services.cli.v44_real01 import main as v44_main
from services.job_runner.state_models import StageRunRow
from services.job_runner.state_store import StateStore
from services.metrics.v44_gate_state import V1ObservationRecord

RUN_ID = "550e8400-e29b-41d4-a716-446655440000"
SEEDED_EP = "ep-seeded-acceptance"
REAL_EP = "ep-real-observation"
CREATED_AT = "2026-08-23T00:00:00+00:00"


def _seeded_workspace(tmp_path: Path) -> tuple[str, Path, Path]:
    episodes_root = tmp_path / "episodes"
    state_store = tmp_path / "state.db"
    episodes_root.mkdir(parents=True, exist_ok=True)
    episode_id = SEEDED_EP
    with StateStore.open(state_store) as store:
        store.create_job(job_id=episode_id, episode_id=episode_id, current_stage="intake")  # type: ignore[arg-type]
        for stage in (
            "ingest",
            "normalize",
            "analyze",
            "selection",
            "plan",
            "compile",
            "preview",
        ):
            store.record_stage_run(
                StageRunRow(
                    job_id=episode_id,  # type: ignore[arg-type]
                    stage_name=stage,  # type: ignore[arg-type]
                    idempotency_key=f"{stage}-run-1",  # type: ignore[arg-type]
                    input_artifact_hashes=(),
                    adopted_artifact_hash=None,
                    status="succeeded",  # type: ignore[arg-type]
                )
            )
    conn = sqlite3.connect(state_store)
    conn.execute(
        "UPDATE jobs SET status='PREVIEW_READY', current_stage='preview' WHERE job_id=?",
        (episode_id,),
    )
    conn.commit()
    conn.close()
    ep_dir = episodes_root / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "brief.json").write_text(
        json.dumps({"schema_version": "cockpit-brief-draft-v1", "episode_id": episode_id}) + "\n",
        encoding="utf-8",
    )
    # Seeded preview clip (e2e seedPreviewClip — existence alone must NOT certify)
    preview_dir = ep_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_file = preview_dir / "preview.mp4"
    preview_file.write_bytes(b"\x00\x00\x00\x00 fake preview")
    intake_path = ep_dir / "intake.json"
    intake_path.write_text(
        json.dumps(
            {
                "schema_version": "cockpit-intake-v1",
                "episode_id": episode_id,
                "source_folder": "/tmp/src",  # noqa: S108
                "brief_text": "seeded",
                "created_at": CREATED_AT,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    created_epoch = datetime.fromisoformat(CREATED_AT).timestamp()
    os.utime(preview_file, (created_epoch + 10, created_epoch + 10))
    return episode_id, episodes_root, state_store


def _real_workspace(
    tmp_path: Path, *, with_leak: bool = False, chat_text: str | None = None
) -> tuple[str, Path, Path, Path]:
    episodes_root = tmp_path / "episodes"
    state_store = tmp_path / "state.db"
    episodes_root.mkdir(parents=True, exist_ok=True)
    episode_id = REAL_EP
    with StateStore.open(state_store) as store:
        store.create_job(job_id=episode_id, episode_id=episode_id, current_stage="intake")  # type: ignore[arg-type]
        for stage in (
            "ingest",
            "normalize",
            "analyze",
            "selection",
            "plan",
            "compile",
            "preview",
        ):
            store.record_stage_run(
                StageRunRow(
                    job_id=episode_id,  # type: ignore[arg-type]
                    stage_name=stage,  # type: ignore[arg-type]
                    idempotency_key=f"{RUNNER_VERSION}:{RUN_ID}:{stage}",  # type: ignore[arg-type]
                    input_artifact_hashes=(),
                    adopted_artifact_hash=None,
                    status="succeeded",  # type: ignore[arg-type]
                )
            )
    conn = sqlite3.connect(state_store)
    conn.execute(
        "UPDATE jobs SET status='PREVIEW_READY', current_stage='preview' WHERE job_id=?",
        (episode_id,),
    )
    conn.commit()
    conn.close()
    ep_dir = episodes_root / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "runner.log").write_text(
        '{"ts":"2026-08-23T00:00:00+00:00","event":"stage","stage":"ingest"}\n',
        encoding="utf-8",
    )
    intake_path = ep_dir / "intake.json"
    intake_path.write_text(
        json.dumps(
            {
                "schema_version": "cockpit-intake-v1",
                "episode_id": episode_id,
                "source_folder": "/tmp/src",  # noqa: S108
                "brief_text": "real",
                "created_at": CREATED_AT,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    preview_dir = ep_dir / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_file = preview_dir / "preview.mp4"
    preview_file.write_bytes(b"\x00\x00\x00\x00 real preview")
    created_epoch = datetime.fromisoformat(CREATED_AT).timestamp()
    os.utime(preview_file, (created_epoch + 10, created_epoch + 10))
    effective_text = chat_text if chat_text is not None else (
        "jobs/ep-real/artifacts/plan.json を使って" if with_leak else "この後2秒残して"
    )
    chat_line = {
        "schema_version": "cockpit-review-chat-v1",
        "sequence": 1,
        "text": effective_text,
        "at_seconds": 1.0,
    }
    (ep_dir / "review-chat.jsonl").write_text(
        json.dumps(chat_line, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    applied_line = {
        "schema_version": "cockpit-applied-command-v1",
        "command_id": "cmd-keep-longer-001",
        "command_kind": "keep_longer",
        "affected_domain": "edit_plan",
        "deferred": False,
    }
    (ep_dir / "applied-commands.jsonl").write_text(
        json.dumps(applied_line, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    metrics_line = {
        "schema_version": "cockpit-rebuild-metric-v1",
        "sequence": 1,
        "applied_command": "cmd-keep-longer-001",
        "stages": ["plan", "compile", "preview"],
        "interpretation_ms": None,
        "rebuild_wall_clock_seconds": 1.42,
        "unrelated_stages_skipped": ["ingest", "normalize", "analyze", "selection"],
        "confirmations": 1,
        "at": (datetime.now(tz=UTC).isoformat()),
    }
    (ep_dir / "rebuild-metrics.jsonl").write_text(
        json.dumps(metrics_line, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return episode_id, episodes_root, state_store, ep_dir


def test_observe_refuses_seeded_no_runner_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    episode_id, episodes_root, state_store = _seeded_workspace(tmp_path)
    out_dir = tmp_path / "out-seeded"
    code = v44_main(
        [
            "observe-v44-1",
            "--episode",
            episode_id,
            "--out",
            str(out_dir),
            "--episodes-root",
            str(episodes_root),
            "--state-store",
            str(state_store),
        ]
    )
    assert code == 3
    captured = capsys.readouterr()
    assert "seeded-state-refused" in (captured.err + captured.out)
    assert not (out_dir / "observation.json").exists()


def test_observe_refuses_fake_runner_log_without_lineage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    episode_id, episodes_root, state_store = _seeded_workspace(tmp_path)
    # Hand-craft a runner.log without stage-run lineage — still seeded
    (episodes_root / episode_id / "runner.log").write_text("fake log\n", encoding="utf-8")
    out_dir = tmp_path / "out-fake"
    code = v44_main(
        [
            "observe-v44-1",
            "--episode",
            episode_id,
            "--out",
            str(out_dir),
            "--episodes-root",
            str(episodes_root),
            "--state-store",
            str(state_store),
        ]
    )
    assert code == 3
    captured = capsys.readouterr()
    assert "seeded-state-refused" in (captured.err + captured.out)
    assert not (out_dir / "observation.json").exists()


def test_observe_writes_valid_observation_for_real_workspace(tmp_path: Path) -> None:
    episode_id, episodes_root, state_store, _ = _real_workspace(tmp_path)
    out_dir = tmp_path / "out-real"
    code = v44_main(
        [
            "observe-v44-1",
            "--episode",
            episode_id,
            "--out",
            str(out_dir),
            "--episodes-root",
            str(episodes_root),
            "--state-store",
            str(state_store),
        ]
    )
    assert code == 0
    observation_path = out_dir / "observation.json"
    assert observation_path.is_file()
    record = V1ObservationRecord.model_validate_json(observation_path.read_bytes())
    assert record.schema_version == "v44-1-observation-v1"
    assert record.episode_id == episode_id
    assert record.stage_timeline.job_status == "PREVIEW_READY"
    assert len(record.stage_timeline.runs) == 7
    assert record.ttfrp_seconds is not None
    assert 9.5 < record.ttfrp_seconds < 10.5
    assert len(record.corrections) == 1
    assert record.corrections[0].applied is True
    assert record.corrections[0].rebuild_wall_clock_seconds == 1.42
    assert len(record.rebuild_records) == 1
    assert record.rebuild_records[0].applied_command == "cmd-keep-longer-001"
    assert record.internal_path_leak is False
    assert record.observed_at != ""


def test_observe_leak_true_when_chat_contains_internal_path(tmp_path: Path) -> None:
    episode_id, episodes_root, state_store, _ = _real_workspace(tmp_path, with_leak=True)
    out_dir = tmp_path / "out-leak"
    code = v44_main(
        [
            "observe-v44-1",
            "--episode",
            episode_id,
            "--out",
            str(out_dir),
            "--episodes-root",
            str(episodes_root),
            "--state-store",
            str(state_store),
        ]
    )
    assert code == 0
    record = V1ObservationRecord.model_validate_json((out_dir / "observation.json").read_bytes())
    assert record.internal_path_leak is True


def test_observe_ttfrp_null_when_preview_missing(tmp_path: Path) -> None:
    episode_id, episodes_root, state_store, ep_dir = _real_workspace(tmp_path)
    (ep_dir / "previews" / "preview.mp4").unlink()
    out_dir = tmp_path / "out-no-preview"
    code = v44_main(
        [
            "observe-v44-1",
            "--episode",
            episode_id,
            "--out",
            str(out_dir),
            "--episodes-root",
            str(episodes_root),
            "--state-store",
            str(state_store),
        ]
    )
    assert code == 0
    record = V1ObservationRecord.model_validate_json((out_dir / "observation.json").read_bytes())
    assert record.ttfrp_seconds is None


def test_record_operator_note_appends_and_observe_includes_it(tmp_path: Path) -> None:
    episode_id, episodes_root, state_store, _ = _real_workspace(tmp_path)
    note_text = "撮影の手触りは良い。この選択で続行したい。"
    code = v44_main(
        [
            "record-operator-note",
            "--episode",
            episode_id,
            "--note",
            note_text,
            "--episodes-root",
            str(episodes_root),
        ]
    )
    assert code == 0
    out_dir = tmp_path / "out-with-note"
    code = v44_main(
        [
            "observe-v44-1",
            "--episode",
            episode_id,
            "--out",
            str(out_dir),
            "--episodes-root",
            str(episodes_root),
            "--state-store",
            str(state_store),
        ]
    )
    assert code == 0
    record = V1ObservationRecord.model_validate_json((out_dir / "observation.json").read_bytes())
    assert record.operator_note == note_text


def test_record_operator_note_empty_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    episode_id, episodes_root, _, _ = _real_workspace(tmp_path)
    code = v44_main(
        [
            "record-operator-note",
            "--episode",
            episode_id,
            "--note",
            "   ",
            "--episodes-root",
            str(episodes_root),
        ]
    )
    assert code != 0
    captured = capsys.readouterr()
    assert "note-empty" in (captured.err + captured.out)


def test_pure_builder_refuses_seeded_inputs() -> None:
    inputs = EpisodeObservationInputs(
        episode_id="ep-pure-seeded",  # type: ignore[arg-type]
        job_status="PREVIEW_READY",
        current_stage="preview",
        stage_runs=(
            StageRunForObservation(
                stage_name="ingest",  # type: ignore[arg-type]
                status="succeeded",  # type: ignore[arg-type]
                retry_count=0,
                idempotency_key="ingest-run-1",  # type: ignore[arg-type]
            ),
        ),
        runner_log_exists=False,
        intake_created_at=None,
        preview_mtime_epoch=None,
        chat_entries=(),
        has_applied_command=False,
        applied_count=0,
        rebuild_metrics=(),
        operator_note=None,
    )
    with pytest.raises(V44ObserveError) as exc:
        build_observation_record(inputs)
    assert exc.value.code == "seeded-state-refused"


def test_pure_builder_ttfrp_computed_and_leak_scanned() -> None:
    now = datetime.now(tz=UTC)
    created = (now - timedelta(seconds=10)).isoformat()
    preview_epoch = now.timestamp()
    inputs = EpisodeObservationInputs(
        episode_id="ep-pure-real",  # type: ignore[arg-type]
        job_status="PREVIEW_READY",
        current_stage="preview",
        stage_runs=(
            StageRunForObservation(
                stage_name="preview",  # type: ignore[arg-type]
                status="succeeded",  # type: ignore[arg-type]
                retry_count=0,
                idempotency_key=f"{RUNNER_VERSION}:{RUN_ID}:preview",  # type: ignore[arg-type]
            ),
        ),
        runner_log_exists=True,
        intake_created_at=created,
        preview_mtime_epoch=preview_epoch,
        chat_entries=(("jobs/foo/artifacts/x.json を見て", 0.5),),
        has_applied_command=True,
        applied_count=1,
        rebuild_metrics=(
            RebuildMetricForObservation(
                sequence=1,
                applied_command="cmd-1",  # type: ignore[arg-type]
                stages=("plan", "compile", "preview"),
                rebuild_wall_clock_seconds=2.3,
                unrelated_stages_skipped=("ingest",),
                at=now.isoformat(),
            ),
        ),
        operator_note=None,
    )
    record = build_observation_record(inputs)
    assert record.ttfrp_seconds is not None
    assert 9 < record.ttfrp_seconds < 11
    assert record.internal_path_leak is True
    assert len(record.corrections) == 1
    assert record.corrections[0].rebuild_wall_clock_seconds == 2.3
