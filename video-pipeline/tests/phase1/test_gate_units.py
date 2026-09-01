from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from services.job_runner.gate_cp_models import OperationOutcome
from services.job_runner.gate_p1_checks import CheckState, check_approvals
from services.job_runner.gate_p1_flow_checks import check_coverage
from services.job_runner.gate_p1_models import FixtureObservation, ReviewStepRecord


def _observation(steps: tuple[ReviewStepRecord, ...]) -> FixtureObservation:
    return FixtureObservation(
        fixture_id="p1-ref-05-review-mix",
        work_dir=".",
        run1_dir=".",
        run2_dir=".",
        operations=(OperationOutcome(name="chain-run", result="ok", detail="x:PREVIEW_READY"),),
        review_steps=steps,
        schema_gap_refused=True,
    )


def _step(index: int, *, structured: bool) -> ReviewStepRecord:
    return ReviewStepRecord(
        command_index=index,
        operation="correct_subtitle",
        status="proposal" if structured else "error",
        classification="clear" if structured else None,
        decision="applied" if structured else "error",
        structured=structured,
    )


class _GoldenOutcome(TypedDict):
    command_index: int
    classification: str
    decision: str
    target_candidate_ids: list[str]


class _GoldenOutcomes(TypedDict):
    review_outcomes: list[_GoldenOutcome]


def _golden_outcomes(count: int) -> _GoldenOutcomes:
    return {
        "review_outcomes": [
            {
                "command_index": index,
                "classification": "clear",
                "decision": "apply",
                "target_candidate_ids": [],
            }
            for index in range(1, count + 1)
        ]
    }


def test_coverage_threshold_is_integer_exact() -> None:
    state = CheckState()
    full = _observation(
        (
            _step(1, structured=True),
            _step(2, structured=True),
            _step(3, structured=True),
            _step(4, structured=True),
        )
    )
    check_coverage(
        {"p1-ref-05-review-mix": full},
        {"p1-ref-05-review-mix": _golden_outcomes(4)},
        state,
    )
    assert state.criteria["phase-1-review-structured-coverage"] is True
    assert state.evidence["phase-1-review-structured-coverage"]


def test_coverage_below_threshold_fails_closed() -> None:
    state = CheckState()
    three_of_four = _observation(
        (
            _step(1, structured=True),
            _step(2, structured=True),
            _step(3, structured=True),
            _step(4, structured=False),
        )
    )
    check_coverage(
        {"p1-ref-05-review-mix": three_of_four},
        {"p1-ref-05-review-mix": _golden_outcomes(4)},
        state,
    )
    assert state.criteria["phase-1-review-structured-coverage"] is False
    assert any(row.code == "coverage-below-80" for row in state.mismatches)


def test_zero_declared_commands_fail_closed() -> None:
    state = CheckState()
    empty = _observation(())
    check_coverage(
        {"p1-ref-01-clean-ja": empty}, {"p1-ref-01-clean-ja": _golden_outcomes(0)}, state
    )
    assert state.criteria["phase-1-review-structured-coverage"] is False


def _approvals_file(path: Path, probes: list[dict[str, str]], fixture_only: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "fixture_id": "approvals",
                "kind": "approval-ingress",
                "work_dir": str(path.parent),
                "operations": probes,
                "fields": {"fixture_record_fixture_only": fixture_only},
            }
        )
    )
    return path


_OK_PROBES = [
    {"name": "automation-ingress-refused", "result": "automation-refused", "detail": ""},
    {"name": "non-tty-ingress-refused", "result": "not-a-tty", "detail": ""},
    {"name": "operator-gate-fixture-refused", "result": "fixture-record", "detail": ""},
]


def test_auto_created_operator_record_fails_the_gate(tmp_path: Path) -> None:
    state = CheckState()
    probes = [dict(_OK_PROBES[0], result="unexpected-success"), *_OK_PROBES[1:]]
    check_approvals(_approvals_file(tmp_path / "o.json", probes, "true"), state)
    assert state.criteria["phase-1-fixtures-100-golden-match"] is False
    assert any(row.code == "operator-record-auto-created" for row in state.mismatches)


def test_unmarked_gate_record_fails_the_gate(tmp_path: Path) -> None:
    state = CheckState()
    check_approvals(_approvals_file(tmp_path / "o.json", _OK_PROBES, "false"), state)
    assert any(row.code == "fixture-label-missing" for row in state.mismatches)
