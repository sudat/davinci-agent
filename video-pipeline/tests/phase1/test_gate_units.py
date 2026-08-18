"""Offline gate-logic units for the Phase-1 Technical Gate (Todo 46).

Drives the REAL evaluator over synthesized golden-derived evidence with one
injected defect per fault knob (no chain run), asserts the coverage
arithmetic and the approval-ingress matrix, and proves the checkpoint
``verify`` surface refuses synthetic-as-real claims. The full real-chain gate
test lives in ``test_gate.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.cli.checkpoint import main as checkpoint_main
from services.cli.checkpoint_models import DisplayReceipt, DisplayTargets, OperatorCheckpoint
from services.foundation_io import canonical_model_bytes
from services.job_runner.gate_cp_models import OperationOutcome
from services.job_runner.gate_p1_checks import CheckState, check_approvals
from services.job_runner.gate_p1_faults import run_fault_cli
from services.job_runner.gate_p1_flow_checks import check_coverage
from services.job_runner.gate_p1_models import FixtureObservation, ReviewStepRecord

FAULTS = (
    "incomplete_flow",
    "must_include_miss",
    "coordinate_defect",
    "coverage_below_80",
    "auto_created_operator_record",
)


@pytest.mark.parametrize("fault", FAULTS)
def test_fault_is_detected_from_recomputed_evidence(tmp_path: Path, fault: str) -> None:
    fixture = tmp_path / f"{fault}.json"
    fixture.write_text(json.dumps({"fault": fault}))
    assert run_fault_cli(fixture, Path("config/gates/phase-1-technical-v1.json")) == 0


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


def _golden_outcomes(count: int) -> dict[str, object]:
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


def _receipt() -> DisplayReceipt:
    targets = DisplayTargets(
        episode_id="real-owner-episode-01",
        stage="PREVIEW_READY",
        plan_version="v1",
        plan_sha256="0" * 64,
        ir_sha256="1" * 64,
        preview_sha256="2" * 64,
        trace_sha256="3" * 64,
        edit_source_world_sha256="4" * 64,
    )
    return DisplayReceipt(
        purpose="EDITORIAL_APPROVED",
        targets=targets,
        target_bundle_sha256=_digest(targets),
    )


def _digest(targets: DisplayTargets) -> str:
    return hashlib.sha256(canonical_model_bytes(targets)).hexdigest()


def _checkpoint(receipt: DisplayReceipt, episode: str) -> OperatorCheckpoint:
    return OperatorCheckpoint(
        schema_version="operator-checkpoint-v1",
        purpose="EDITORIAL_APPROVED",
        episode_id=episode,
        fixture_only=False,
        eligibility_status="supported",
        fixture_manifest_sha256="0" * 64,
        edit_source_world_sha256="0" * 64,
        media_sha256=("0" * 64,),
        initial_plan_sha256="0" * 64,
        initial_ir_sha256="0" * 64,
        initial_preview_sha256="0" * 64,
        final_plan_sha256="0" * 64,
        final_ir_sha256="0" * 64,
        final_preview_sha256="0" * 64,
        event_chain=(),
        toolchain_lock_sha256="0" * 64,
        translator_policy_sha256="0" * 64,
        production_policy_sha256="0" * 64,
        displayed_target_sha256=receipt.target_bundle_sha256,
        display_receipt_sha256=hashlib.sha256(receipt.canonical_bytes()).hexdigest(),
        operation_record_id="op-1",
        operation_record_sha256="0" * 64,
        actor_id="local-operator",
        uid=501,
        tty="/dev/ttys000",
        wall_time_unix=1,
    )


def _write_pair(tmp_path: Path, episode: str) -> tuple[Path, Path]:
    receipt = _receipt()
    checkpoint = _checkpoint(receipt, episode)
    receipt_path = tmp_path / "display.json"
    checkpoint_path = tmp_path / "checkpoint.json"
    receipt_path.write_bytes(receipt.canonical_bytes())
    checkpoint_path.write_bytes(checkpoint.canonical_bytes())
    return checkpoint_path, receipt_path


def test_verify_accepts_a_consistent_real_episode_checkpoint(tmp_path: Path) -> None:
    checkpoint_path, receipt_path = _write_pair(tmp_path, "real-owner-episode-01")
    code = checkpoint_main(
        [
            "verify",
            "--checkpoint",
            str(checkpoint_path),
            "--display-receipt",
            str(receipt_path),
            "--require-purpose",
            "EDITORIAL_APPROVED",
            "--require-real-episode",
            "--recompute",
        ]
    )
    assert code == 0


def test_verify_refuses_synthetic_episode_claimed_as_real(tmp_path: Path) -> None:
    checkpoint_path, receipt_path = _write_pair(tmp_path, "p1-ref-05-review-mix")
    code = checkpoint_main(
        [
            "verify",
            "--checkpoint",
            str(checkpoint_path),
            "--display-receipt",
            str(receipt_path),
            "--require-purpose",
            "EDITORIAL_APPROVED",
            "--require-real-episode",
            "--recompute",
        ]
    )
    assert code == 1


def test_verify_refuses_drifted_receipt_bytes(tmp_path: Path) -> None:
    checkpoint_path, receipt_path = _write_pair(tmp_path, "real-owner-episode-01")
    drifted = json.loads(receipt_path.read_bytes())
    drifted["targets"]["plan_version"] = "v2"
    receipt_path.write_text(json.dumps(drifted, sort_keys=True, separators=(",", ":")))
    code = checkpoint_main(
        [
            "verify",
            "--checkpoint",
            str(checkpoint_path),
            "--display-receipt",
            str(receipt_path),
            "--require-purpose",
            "EDITORIAL_APPROVED",
            "--require-real-episode",
            "--recompute",
        ]
    )
    assert code == 1


def test_verify_refuses_fixture_marked_checkpoint(tmp_path: Path) -> None:
    checkpoint_path, receipt_path = _write_pair(tmp_path, "real-owner-episode-01")
    forged = json.loads(checkpoint_path.read_bytes())
    forged["fixture_only"] = True
    checkpoint_path.write_text(json.dumps(forged, sort_keys=True, separators=(",", ":")))
    code = checkpoint_main(
        [
            "verify",
            "--checkpoint",
            str(checkpoint_path),
            "--display-receipt",
            str(receipt_path),
            "--require-purpose",
            "EDITORIAL_APPROVED",
            "--require-real-episode",
            "--recompute",
        ]
    )
    assert code == 1
