from __future__ import annotations

import hashlib
import json
from pathlib import Path

from services.gates import GateResult
from tests.control_plane_gate.support import (
    MANIFEST_DIR,
    TOOLCHAIN_LOCK,
    run_gate_cli,
    synthetic_parent_result,
    synthetic_policy,
)


def cli_args(tmp_path: Path, evidence: Path, *extra: str) -> list[str]:
    parent = synthetic_parent_result(tmp_path / "inputs")
    policy_path, _sha = synthetic_policy(
        tmp_path / "inputs",
        parent_sha256=hashlib.sha256(parent.read_bytes()).hexdigest(),
    )
    return [
        "control-plane-baseline",
        "--policy",
        str(policy_path),
        "--evidence",
        str(evidence),
        "--parent-result",
        str(parent),
        "--manifest-dir",
        str(MANIFEST_DIR),
        "--toolchain-lock",
        str(TOOLCHAIN_LOCK),
        *extra,
    ]


def test_cli_green_exit_zero(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    result = run_gate_cli(*cli_args(tmp_path, evidence))
    assert result.returncode == 0, result.stderr
    assert "cp-gate: PASS" in result.stdout
    gate_result = GateResult.model_validate_json(
        (evidence / "gate-result.json").read_bytes()
    )
    assert gate_result.passed is True
    assert len(gate_result.criteria_results) == 5


def test_cli_tty_fixture_green_exit_zero(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    result = run_gate_cli(*cli_args(tmp_path, evidence, "--tty-fixture"))
    assert result.returncode == 0, result.stderr
    assert "cp-gate: PASS" in result.stdout


def test_cli_rerun_idempotent(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    first = run_gate_cli(*cli_args(tmp_path, evidence))
    second = run_gate_cli(*cli_args(tmp_path, evidence, "--tty-fixture"))
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr


def test_cli_wrong_parent_binding_exit_one(tmp_path: Path) -> None:
    parent = synthetic_parent_result(tmp_path / "inputs")
    policy_path, _sha = synthetic_policy(tmp_path / "inputs", parent_sha256="7" * 64)
    result = run_gate_cli(
        "control-plane-baseline",
        "--policy",
        str(policy_path),
        "--evidence",
        str(tmp_path / "evidence"),
        "--parent-result",
        str(parent),
        "--manifest-dir",
        str(MANIFEST_DIR),
        "--toolchain-lock",
        str(TOOLCHAIN_LOCK),
    )
    assert result.returncode == 1
    assert "cp-gate: FAIL" in result.stdout
    assert "parent-gate-drift" in result.stdout


def test_cli_noncanonical_policy_exit_two(tmp_path: Path) -> None:
    parent = synthetic_parent_result(tmp_path / "inputs")
    policy_path, _sha = synthetic_policy(
        tmp_path / "inputs",
        parent_sha256=hashlib.sha256(parent.read_bytes()).hexdigest(),
    )
    payload = json.loads(policy_path.read_text())
    policy_path.write_text(json.dumps(payload, indent=2))
    result = run_gate_cli(
        "control-plane-baseline",
        "--policy",
        str(policy_path),
        "--evidence",
        str(tmp_path / "evidence"),
        "--parent-result",
        str(parent),
    )
    assert result.returncode == 2
    assert "policy-noncanonical" in result.stderr


def test_cli_missing_parent_exit_one(tmp_path: Path) -> None:
    policy_path, _sha = synthetic_policy(tmp_path / "inputs", parent_sha256="8" * 64)
    result = run_gate_cli(
        "control-plane-baseline",
        "--policy",
        str(policy_path),
        "--evidence",
        str(tmp_path / "orphan-evidence"),
    )
    assert result.returncode == 1
    assert "parent-unresolvable" in result.stderr


def test_cli_missing_evidence_usage_exit_two() -> None:
    result = run_gate_cli("control-plane-baseline", "--policy", "whatever.json")
    assert result.returncode == 2
