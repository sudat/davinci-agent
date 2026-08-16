from __future__ import annotations

import json
import sys
from pathlib import Path

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
