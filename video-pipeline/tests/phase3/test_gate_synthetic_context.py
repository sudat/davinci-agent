"""Synthetic-context trust boundary for the Phase-3 fault harness.

The context derives a synthetic policy from the supplied frozen policy
(changing exactly the gate version, the parent binding, and the toolchain
lock), pairs it with one canonical clearly synthetic phase-2 parent, fails
closed on any selected-lock drift, and leaves no persistent outputs.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Literal

import pytest

from services.foundation_io import sha256_file
from services.gates import GatePolicy, canonical_gate_bytes
from services.gates.phase2 import PHASE_2_CRITERIA
from services.job_runner import gate_p3_checks, gate_p3_faults, gate_phase3
from services.job_runner.gate_p3_fake_tree import FaultKnobs
from services.job_runner.gate_p3_models import SYNTHETIC_VERSION
from services.job_runner.gate_p3_regression import phase2_policy
from services.job_runner.gate_p3_scan import resolve_repo_path

POLICY = Path("config/gates/phase-3-v3.json")


def test_phase2_policy_is_pinned_to_v1() -> None:
    assert phase2_policy() == Path("config/gates/phase-2-v1.json")


def test_synthetic_parent_is_canonical_clearly_marked_and_bound() -> None:
    context = gate_p3_faults._build_context(POLICY)
    parent = context.parent

    assert (parent.result.gate_id, parent.relative) == ("phase-2", "phase-2")
    assert parent.raw == canonical_gate_bytes(parent.result)
    assert hashlib.sha256(parent.raw).hexdigest() == parent.sha256
    assert "synthetic" in parent.result.gate_version
    assert parent.result.passed is True
    assert parent.result.policy_sha256 == sha256_file(resolve_repo_path(phase2_policy()))
    assert tuple(row.criterion_id for row in parent.result.criteria_results) == PHASE_2_CRITERIA


def test_synthetic_policy_changes_only_transient_bindings() -> None:
    source, source_sha256 = gate_phase3.load_policy(POLICY)
    context = gate_p3_faults._build_context(POLICY)
    source_fields = source.model_dump(mode="json")
    synthetic_fields = context.policy.model_dump(mode="json")
    for field in ("gate_version", "parent_gate_result_hashes", "toolchain_lock_sha256"):
        del source_fields[field]
        del synthetic_fields[field]

    assert synthetic_fields == source_fields
    assert context.policy.gate_version == SYNTHETIC_VERSION
    assert context.policy.parent_gate_result_hashes == (context.parent.sha256,)
    assert context.policy.toolchain_lock_sha256 == sha256_file(
        resolve_repo_path(gate_p3_checks.LOCK_PATH)
    )
    assert context.policy_bytes == canonical_gate_bytes(context.policy)
    assert context.policy_sha256 == hashlib.sha256(context.policy_bytes).hexdigest()
    assert context.policy_sha256 != source_sha256


def test_stale_source_toolchain_is_rebound_to_selected_lock(tmp_path: Path) -> None:
    selected_sha256 = sha256_file(resolve_repo_path(gate_p3_checks.LOCK_PATH))
    stale_sha256 = hashlib.sha256(b"test:deliberately-stale-toolchain").hexdigest()
    supplied = _policy_with_binding(tmp_path, "toolchain_lock_sha256", stale_sha256)

    context = gate_p3_faults._build_context(supplied)

    assert stale_sha256 != selected_sha256
    assert context.policy.toolchain_lock_sha256 == selected_sha256


def test_context_and_evaluator_share_monkeypatched_selected_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "selected-toolchain.json"
    selected.write_bytes(b"synthetic selected toolchain lock")
    monkeypatch.setattr(gate_p3_checks, "LOCK_PATH", selected)
    context = gate_p3_faults._build_context(POLICY)

    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        outcome = gate_p3_faults._evaluate_fake(
            Path(temporary), FaultKnobs(fault=""), context
        )

    assert outcome.result.passed, outcome.mismatches
    assert context.policy.toolchain_lock_sha256 == sha256_file(selected)


def test_selected_lock_change_between_baseline_and_probe_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "fault.json"
    fixture.write_text(json.dumps({"fault": "stale_asset"}))
    selected = tmp_path / "selected-toolchain.json"
    selected.write_bytes(b"selected toolchain before baseline")
    monkeypatch.setattr(gate_p3_checks, "LOCK_PATH", selected)
    real_evaluate_fake = gate_p3_faults._evaluate_fake

    def evaluate_then_change_lock(
        root: Path,
        knobs: FaultKnobs,
        context: gate_p3_faults._FaultContext,
    ) -> gate_phase3.Phase3GateOutcome:
        outcome = real_evaluate_fake(root, knobs, context)
        if knobs.fault == "":
            assert outcome.result.passed, outcome.mismatches
            selected.write_bytes(b"selected toolchain changed after baseline")
        return outcome

    monkeypatch.setattr(gate_p3_faults, "_evaluate_fake", evaluate_then_change_lock)

    code = gate_p3_faults.run_fault_cli(fixture, POLICY)

    captured = capsys.readouterr()
    assert code == 2
    assert "baseline=PASS" in captured.out
    assert "detected=false" in captured.out
    assert "toolchain-lock-drift" in captured.out


def test_non_phase3_policy_returns_typed_exit_two_without_detection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, _source_sha256 = gate_phase3.load_policy(POLICY)
    document = source.model_dump(mode="json")
    document["gate_id"] = "generic-synthetic-gate"
    generic = GatePolicy.model_validate(document)
    policy_path = tmp_path / "generic-policy.json"
    policy_path.write_bytes(canonical_gate_bytes(generic))
    fixture = tmp_path / "fault.json"
    fixture.write_text(json.dumps({"fault": "stale_asset"}))

    code = gate_p3_faults.run_fault_cli(fixture, policy_path)

    captured = capsys.readouterr()
    assert code == 2
    assert "fault-error policy-gate-mismatch" in captured.err
    assert "Traceback" not in captured.err
    assert "detected=true" not in captured.out


def test_synthetic_context_bytes_are_deterministic() -> None:
    first = gate_p3_faults._build_context(POLICY)
    second = gate_p3_faults._build_context(POLICY)

    assert first.policy_bytes == second.policy_bytes
    assert first.parent.raw == second.parent.raw


def test_context_uses_the_supplied_policy_path(tmp_path: Path) -> None:
    binding = hashlib.sha256(b"test:supplied-policy-binding").hexdigest()
    supplied = _policy_with_binding(tmp_path, "fixture_manifest_sha256", binding)

    context = gate_p3_faults._build_context(supplied)

    assert context.policy.fixture_manifest_sha256 == binding


def test_missing_policy_returns_exit_two_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "fault.json"
    fixture.write_text(json.dumps({"fault": "stale_asset"}))
    missing = tmp_path / "missing-policy.json"

    code = gate_p3_faults.run_fault_cli(fixture, missing)

    captured = capsys.readouterr()
    assert code == 2
    assert "fault-error" in captured.err
    assert str(missing) in captured.err
    assert "Traceback" not in captured.err


def test_broken_baseline_cannot_report_fault_detected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "fault.json"
    fixture.write_text(json.dumps({"fault": "stale_asset"}))
    broken = _policy_with_binding(
        tmp_path,
        "fixture_manifest_sha256",
        hashlib.sha256(b"test:broken-baseline").hexdigest(),
    )

    code = gate_p3_faults.run_fault_cli(fixture, broken)

    captured = capsys.readouterr()
    assert code == 2
    assert "baseline=FAIL" in captured.out
    assert "detected=false" in captured.out
    assert "Traceback" not in captured.err


def test_synthetic_files_are_ephemeral_and_identity_is_not_frozen(tmp_path: Path) -> None:
    source_bytes = POLICY.read_bytes()
    lock_path = resolve_repo_path(gate_p3_checks.LOCK_PATH)
    lock_bytes = lock_path.read_bytes()
    _source, source_sha256 = gate_phase3.load_policy(POLICY)
    context = gate_p3_faults._build_context(POLICY)

    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        root = Path(temporary)
        outcome = gate_p3_faults._evaluate_fake(root, FaultKnobs(fault=""), context)
        assert outcome.result.passed, outcome.mismatches
        assert outcome.result.gate_version == SYNTHETIC_VERSION
        assert outcome.result.policy_sha256 == context.policy_sha256
        assert outcome.result.policy_sha256 != source_sha256
        assert (root / "phase-2" / "gate-result.json").is_file()

    assert not root.exists()
    assert POLICY.read_bytes() == source_bytes
    assert lock_path.read_bytes() == lock_bytes


def _policy_with_binding(
    tmp_path: Path,
    field: Literal[
        "fixture_manifest_sha256", "golden_sha256", "toolchain_lock_sha256"
    ],
    value: str,
) -> Path:
    source, _source_sha256 = gate_phase3.load_policy(POLICY)
    document = source.model_dump(mode="json")
    document[field] = value
    policy = GatePolicy.model_validate(document)
    path = tmp_path / "policy.json"
    path.write_bytes(canonical_gate_bytes(policy))
    return path
