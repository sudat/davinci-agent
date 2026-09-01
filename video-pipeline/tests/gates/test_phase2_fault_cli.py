"""The phase-2 fault CLI honors the SUPPLIED policy through the synthetic
seam and turns typed refusals into fault-error exits — never a traceback.

Durable replacement for the workspace-anchored round-2 QA regression: the
harness follows the operator-supplied current policy (never the live attempt
directory), detects the injected fault from recomputed evidence, and fails
closed when the selected toolchain lock changes between baseline and probe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.job_runner import gate_p2_checks, gate_p2_faults

POLICY = Path("config/gates/phase-2-v4.json")
WRONG_POLICY = Path("config/gates/phase-2-v1.json")
FAULT_FIXTURE = Path("tests/fixtures/phase2-gate-faults/item_mismatch.json")
EXPECTED_CODE = "item-conformance-defect"

if TYPE_CHECKING:
    from services.job_runner.gate_p2_fake_tree import FaultKnobs


def test_run_fault_cli_honors_the_supplied_current_policy(capsys) -> None:
    code = gate_p2_faults.run_fault_cli(FAULT_FIXTURE, POLICY)
    captured = capsys.readouterr()

    assert code == 0
    assert "phase2-gate mode=synthetic synthetic_policy_sha256=" in captured.out
    assert "baseline=PASS" in captured.out
    assert f"detected=true expected_code={EXPECTED_CODE}" in captured.out


def test_cli_detects_fault_at_current_frozen_chain_state(capsys) -> None:
    fixture = Path("tests/fixtures/phase2-gate-faults/unbounded_retry.json")
    code = gate_p2_faults.run_fault_cli(fixture, POLICY)
    captured = capsys.readouterr()

    assert code == 0
    assert "detected=true expected_code=unbounded-retry" in captured.out
    assert "baseline=PASS" in captured.out


def test_cli_gate_p2_error_is_typed_fault_error_not_traceback(capsys) -> None:
    assert WRONG_POLICY.is_file()

    code = gate_p2_faults.run_fault_cli(FAULT_FIXTURE, WRONG_POLICY)
    captured = capsys.readouterr()

    assert code == 2
    assert "fault-error policy-gate-mismatch" in captured.err
    assert "Traceback" not in captured.err
    assert "detected=true" not in captured.out


def test_cli_malformed_fault_spec_is_typed_refusal(tmp_path: Path, capsys) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    code = gate_p2_faults.run_fault_cli(broken, POLICY)
    captured = capsys.readouterr()
    assert code == 2
    assert "fault-unreadable" in captured.err

    not_object = tmp_path / "list.json"
    not_object.write_text("[1, 2]")
    code = gate_p2_faults.run_fault_cli(not_object, POLICY)
    captured = capsys.readouterr()
    assert code == 2
    assert "fault-unreadable" in captured.err


def test_selected_lock_change_between_baseline_and_probe_fails_closed(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    fixture = tmp_path / "fault.json"
    fixture.write_text(json.dumps({"fault": "item_mismatch"}))
    selected = tmp_path / "selected-toolchain.json"
    selected.write_bytes(b"selected toolchain before baseline")
    monkeypatch.setattr(gate_p2_checks, "LOCK_PATH", selected)
    real_evaluate_fake = gate_p2_faults._evaluate_fake

    def evaluate_then_change_lock(
        root: Path,
        knobs: FaultKnobs,
        context: gate_p2_faults._FaultContext,
    ) -> gate_p2_faults.FakeOutcome:
        outcome = real_evaluate_fake(root, knobs, context)
        if knobs.fault == "":
            assert outcome.result.passed, outcome.mismatches
            selected.write_bytes(b"selected toolchain changed after baseline")
        return outcome

    monkeypatch.setattr(gate_p2_faults, "_evaluate_fake", evaluate_then_change_lock)

    code = gate_p2_faults.run_fault_cli(fixture, POLICY)
    captured = capsys.readouterr()

    assert code == 2
    assert "baseline=PASS" in captured.out
    assert "detected=false" in captured.out
    assert "toolchain-lock-drift" in captured.out
