from __future__ import annotations

from pathlib import Path

from services.job_runner.gate_cp_state import drive_serialized_authority


def outcomes_by_name(tmp_path: Path) -> dict[str, str]:
    rows = drive_serialized_authority(tmp_path / "state")
    return {row.name: row.result for row in rows}


def test_two_writers_one_commits_loser_superseded(tmp_path: Path) -> None:
    results = outcomes_by_name(tmp_path)
    assert results["cas-writer-one-commits"] == "ok"
    assert results["cas-writer-two-superseded"] == "superseded"
    assert results["cas-idempotent-replay"] == "ok"


def test_loser_writes_nothing_on_replay(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    drive_serialized_authority(state_dir)
    replay = drive_serialized_authority(state_dir / "again")
    results = sorted(row.result for row in replay)
    assert results == ["ok", "ok", "superseded"]
