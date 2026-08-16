from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from services.execution.work_init import WorkInitializationError, restore_for_plan
from services.qa.run_todo import run_matrix


def _case(
    expected_exit: int, observation: str
) -> dict[str, str | int | list[str] | dict[str, str] | None]:
    return {
        "case_id": "case-one",
        "kind": "happy",
        "argv": [sys.executable, "-c", "print('visible')"],
        "stdin_fixture": None,
        "environment": {},
        "fault_fixture": None,
        "expected_exit": expected_exit,
        "expected_artifacts": [],
        "expected_observation": observation,
    }


def test_qa_runner_records_green_case(tmp_path: Path) -> None:
    matrix = tmp_path / "matrix.json"
    evidence = tmp_path / "evidence.json"
    matrix.write_text(json.dumps([_case(0, "visible")]))

    observed = run_matrix(1, matrix, evidence, Path(sys.executable), tmp_path)

    assert observed is True
    assert json.loads(evidence.read_text())["all_observed"] is True


def test_qa_runner_records_red_case_and_returns_failure(tmp_path: Path) -> None:
    matrix = tmp_path / "matrix.json"
    evidence = tmp_path / "evidence.json"
    matrix.write_text(json.dumps([_case(1, "missing")]))

    observed = run_matrix(1, matrix, evidence, Path(sys.executable), tmp_path)

    assert observed is False
    case = json.loads(evidence.read_text())["cases"][0]
    assert case["observed"] is False


def _write_initialization(
    tmp_path: Path, *, plan_input: Path
) -> tuple[Path, Path, str]:
    plan = tmp_path / "foundation-video-pipeline.md"
    plan.write_text("live plan\n")
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir(exist_ok=True)
    uv = Path(sys.executable).resolve()
    python = Path(sys.executable).resolve()

    def sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    initial_hash = sha256(plan_input)
    execution_work_id = hashlib.sha256(
        str(plan).encode() + b"\x00" + bytes.fromhex(initial_hash)
    ).hexdigest()
    row = {
        "event": "work-initialized",
        "plan_path": str(plan),
        "initial_plan_sha256": initial_hash,
        "execution_work_id": execution_work_id,
        "attempt_dir": str(attempt_dir),
        "plan_input": str(plan_input),
        "execution_ledger": str(tmp_path / "execution.jsonl"),
        "uv_bin": str(uv),
        "uv_sha256": sha256(uv),
        "python_bin": str(python),
        "python_sha256": sha256(python),
        "created_at": "2026-01-01T00:00:00Z",
    }
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(row) + "\n")
    return ledger, plan, execution_work_id


def test_restore_selects_snapshot_when_live_plan_changed(tmp_path: Path) -> None:
    snapshot = tmp_path / "attempt" / "plan-input.md"
    snapshot.parent.mkdir()
    snapshot.write_text("immutable plan\n")
    ledger, plan, _ = _write_initialization(tmp_path, plan_input=snapshot)
    plan.write_text("live plan with completed checkbox\n")

    restored = restore_for_plan(ledger, plan, "not-the-live-hash")

    assert restored.plan_input == snapshot.resolve()


def test_restore_rejects_tampered_plan_input(tmp_path: Path) -> None:
    snapshot = tmp_path / "attempt" / "plan-input.md"
    snapshot.parent.mkdir()
    snapshot.write_text("immutable plan\n")
    ledger, plan, _ = _write_initialization(tmp_path, plan_input=snapshot)
    snapshot.write_text("tampered plan\n")

    with pytest.raises(WorkInitializationError, match="plan input hash drift"):
        restore_for_plan(ledger, plan, "ignored")


def test_restore_rejects_two_active_rows(tmp_path: Path) -> None:
    snapshot = tmp_path / "attempt" / "plan-input.md"
    snapshot.parent.mkdir()
    snapshot.write_text("immutable plan\n")
    ledger, plan, _ = _write_initialization(tmp_path, plan_input=snapshot)
    second = json.loads(ledger.read_text())
    second["execution_work_id"] = "b" * 64
    ledger.write_text(ledger.read_text() + json.dumps(second) + "\n")

    with pytest.raises(WorkInitializationError, match="found 2"):
        restore_for_plan(ledger, plan, "ignored")
