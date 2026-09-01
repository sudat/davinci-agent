from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Literal

import pytest

from services.foundation_io import sha256_file
from services.gates import GatePolicy, GateResult, canonical_gate_bytes
from services.job_runner import gate_p1_faults, gate_phase1
from services.job_runner.gate_p1_fakes import FaultKnobs
from services.job_runner.gate_p1_faults import run_fault_cli

POLICY = Path("config/gates/phase-1-technical-v1.json")
FAULT_CASES = (
    ("incomplete_flow", "evidence-incomplete"),
    ("must_include_miss", "must-include-miss"),
    ("coordinate_defect", "coordinate-defect"),
    ("coverage_below_80", "coverage-below-80"),
    ("auto_created_operator_record", "operator-record-auto-created"),
)


@pytest.mark.parametrize("case", FAULT_CASES)
def test_fault_is_detected_from_recomputed_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, str],
) -> None:
    fault, expected_code = case
    fixture = tmp_path / f"{fault}.json"
    fixture.write_text(json.dumps({"fault": fault}))

    code = run_fault_cli(fixture, POLICY)

    captured = capsys.readouterr()
    assert code == 0
    assert "phase1-gate mode=synthetic synthetic_policy_sha256=" in captured.out
    assert "baseline=PASS" in captured.out
    assert f"detected=true expected_code={expected_code}" in captured.out


def _result_hashes(result: GateResult) -> set[str]:
    return {
        result.policy_sha256,
        *(bundle.bundle_sha256 for bundle in result.evidence_bundles),
        *(value for bundle in result.evidence_bundles for value in bundle.raw_evidence_sha256s),
        *(value for row in result.criteria_results for value in row.raw_evidence_sha256s),
    }


def _policy_with_binding(
    tmp_path: Path,
    field: Literal[
        "fixture_manifest_sha256", "golden_sha256", "toolchain_lock_sha256"
    ],
    value: str,
) -> Path:
    source, _source_sha256 = gate_phase1.load_policy(POLICY)
    document = source.model_dump(mode="json")
    document[field] = value
    policy = GatePolicy.model_validate(document)
    path = tmp_path / "policy.json"
    path.write_bytes(canonical_gate_bytes(policy))
    return path


def test_synthetic_parents_are_canonical_ordered_and_disjoint() -> None:
    context = gate_p1_faults._build_context(POLICY)

    assert tuple((parent.result.gate_id, parent.relative) for parent in context.parents) == (
        ("phase-0c", "phase-0c"),
        ("control-plane-baseline", "control-plane"),
    )
    assert all(parent.raw == canonical_gate_bytes(parent.result) for parent in context.parents)
    assert all(
        hashlib.sha256(parent.raw).hexdigest() == parent.sha256
        for parent in context.parents
    )
    assert _result_hashes(context.parents[0].result).isdisjoint(
        _result_hashes(context.parents[1].result)
    )
    assert all("synthetic" in parent.result.gate_version for parent in context.parents)


def test_synthetic_policy_changes_only_transient_bindings() -> None:
    source, _source_sha256 = gate_phase1.load_policy(POLICY)
    context = gate_p1_faults._build_context(POLICY)
    source_fields = source.model_dump(mode="json")
    synthetic_fields = context.policy.model_dump(mode="json")
    for field in ("gate_version", "parent_gate_result_hashes", "toolchain_lock_sha256"):
        del source_fields[field]
        del synthetic_fields[field]

    assert synthetic_fields == source_fields
    assert context.policy.gate_version == gate_p1_faults.SYNTHETIC_VERSION
    assert context.policy.parent_gate_result_hashes == tuple(
        parent.sha256 for parent in context.parents
    )
    assert context.policy.toolchain_lock_sha256 == sha256_file(gate_phase1.TOOLCHAIN_LOCK)
    assert context.policy_bytes == canonical_gate_bytes(context.policy)
    assert context.policy_sha256 == hashlib.sha256(context.policy_bytes).hexdigest()


def test_stale_source_toolchain_is_rebound_to_selected_lock(tmp_path: Path) -> None:
    selected_sha256 = sha256_file(gate_phase1.TOOLCHAIN_LOCK)
    stale_sha256 = hashlib.sha256(b"test:deliberately-stale-toolchain").hexdigest()
    supplied = _policy_with_binding(tmp_path, "toolchain_lock_sha256", stale_sha256)

    context = gate_p1_faults._build_context(supplied)

    assert stale_sha256 != selected_sha256
    assert context.policy.toolchain_lock_sha256 == selected_sha256


def test_context_and_evaluator_share_monkeypatched_selected_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "selected-toolchain.json"
    selected.write_bytes(b"synthetic selected toolchain lock")
    monkeypatch.setattr(gate_phase1, "TOOLCHAIN_LOCK", selected)
    context = gate_p1_faults._build_context(POLICY)

    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        outcome = gate_p1_faults._evaluate_fake(
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
    fixture.write_text(json.dumps({"fault": "incomplete_flow"}))
    selected = tmp_path / "selected-toolchain.json"
    selected.write_bytes(b"selected toolchain before baseline")
    monkeypatch.setattr(gate_phase1, "TOOLCHAIN_LOCK", selected)
    real_evaluate_fake = gate_p1_faults._evaluate_fake

    def evaluate_then_change_lock(
        root: Path,
        knobs: FaultKnobs,
        context: gate_p1_faults._FaultContext,
    ) -> gate_phase1.Phase1GateOutcome:
        outcome = real_evaluate_fake(root, knobs, context)
        if knobs.fault == "":
            assert outcome.result.passed, outcome.mismatches
            selected.write_bytes(b"selected toolchain changed after baseline")
        return outcome

    monkeypatch.setattr(gate_p1_faults, "_evaluate_fake", evaluate_then_change_lock)

    code = run_fault_cli(fixture, POLICY)

    captured = capsys.readouterr()
    assert code == 2
    assert "baseline=PASS" in captured.out
    assert "detected=false" in captured.out
    assert "toolchain-lock-drift" in captured.out


def test_non_phase1_policy_returns_typed_exit_two_without_detection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, _source_sha256 = gate_phase1.load_policy(POLICY)
    document = source.model_dump(mode="json")
    document["gate_id"] = "generic-synthetic-gate"
    generic = GatePolicy.model_validate(document)
    policy_path = tmp_path / "generic-policy.json"
    policy_path.write_bytes(canonical_gate_bytes(generic))
    fixture = tmp_path / "fault.json"
    fixture.write_text(json.dumps({"fault": "incomplete_flow"}))

    code = run_fault_cli(fixture, policy_path)

    captured = capsys.readouterr()
    assert code == 2
    assert "fault-error policy-gate-mismatch" in captured.err
    assert "Traceback" not in captured.err
    assert "detected=true" not in captured.out


def test_synthetic_context_bytes_are_deterministic() -> None:
    first = gate_p1_faults._build_context(POLICY)
    second = gate_p1_faults._build_context(POLICY)

    assert first.policy_bytes == second.policy_bytes
    assert tuple(parent.raw for parent in first.parents) == tuple(
        parent.raw for parent in second.parents
    )


def test_context_uses_the_supplied_policy_path(tmp_path: Path) -> None:
    binding = hashlib.sha256(b"test:supplied-policy-binding").hexdigest()
    supplied = _policy_with_binding(tmp_path, "fixture_manifest_sha256", binding)

    context = gate_p1_faults._build_context(supplied)

    assert context.policy.fixture_manifest_sha256 == binding


def test_missing_policy_returns_exit_two_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "fault.json"
    fixture.write_text(json.dumps({"fault": "incomplete_flow"}))
    missing = tmp_path / "missing-policy.json"

    code = run_fault_cli(fixture, missing)

    captured = capsys.readouterr()
    assert code == 2
    assert "fault-error" in captured.err
    assert str(missing) in captured.err
    assert "Traceback" not in captured.err


def test_broken_baseline_cannot_report_fault_detected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "fault.json"
    fixture.write_text(json.dumps({"fault": "incomplete_flow"}))
    broken = _policy_with_binding(
        tmp_path,
        "golden_sha256",
        hashlib.sha256(b"test:broken-baseline").hexdigest(),
    )

    code = run_fault_cli(fixture, broken)

    captured = capsys.readouterr()
    assert code == 2
    assert "baseline=FAIL" in captured.out
    assert "detected=false" in captured.out
    assert "Traceback" not in captured.err


def test_synthetic_files_are_ephemeral_and_identity_is_not_frozen(tmp_path: Path) -> None:
    source_bytes = POLICY.read_bytes()
    selected_lock_bytes = gate_phase1.TOOLCHAIN_LOCK.read_bytes()
    _source, source_sha256 = gate_phase1.load_policy(POLICY)
    context = gate_p1_faults._build_context(POLICY)

    with tempfile.TemporaryDirectory(dir=tmp_path) as temporary:
        root = Path(temporary)
        outcome = gate_p1_faults._evaluate_fake(root, FaultKnobs(fault=""), context)
        assert outcome.result.passed, outcome.mismatches
        assert outcome.result.gate_version == gate_p1_faults.SYNTHETIC_VERSION
        assert outcome.result.policy_sha256 == context.policy_sha256
        assert outcome.result.policy_sha256 != source_sha256
        assert (root / "phase-0c" / "gate-result.json").is_file()
        assert (root / "control-plane" / "gate-result.json").is_file()

    assert not root.exists()
    assert POLICY.read_bytes() == source_bytes
    assert gate_phase1.TOOLCHAIN_LOCK.read_bytes() == selected_lock_bytes
