from __future__ import annotations

import hashlib
import json
from pathlib import Path

from services.gates import GatePolicy
from services.job_runner.gate_cp_approvals import drive_approvals
from services.job_runner.gate_cp_evaluate import RESULT_NAME, evaluate_gate
from services.job_runner.gate_cp_scenarios import MANIFEST_DIR, drive_scenarios
from tests.control_plane_gate.support import (
    TOOLCHAIN_LOCK,
    synthetic_parent_result,
    synthetic_policy,
)


def drive_all(tmp_path: Path, *, tty_fixture: bool = False) -> Path:
    evidence = tmp_path / "evidence"
    drive_scenarios(evidence, MANIFEST_DIR)
    drive_approvals(evidence, tty_fixture=tty_fixture)
    return evidence


def run_evaluation(
    tmp_path: Path,
    evidence: Path,
    *,
    parent_sha256: str | None = None,
    fixture_manifest_sha256: str | None = None,
):
    parent = synthetic_parent_result(tmp_path)
    resolved_parent = parent_sha256 or hashlib.sha256(parent.read_bytes()).hexdigest()
    policy_path, _policy_sha = synthetic_policy(
        tmp_path,
        parent_sha256=resolved_parent,
        fixture_manifest_sha256=fixture_manifest_sha256,
    )
    policy = GatePolicy.model_validate_json(policy_path.read_bytes())
    return evaluate_gate(
        policy,
        policy_sha256=hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        evidence=evidence,
        manifest_dir=MANIFEST_DIR,
        toolchain_lock=TOOLCHAIN_LOCK,
        parent_result=parent,
    )


def test_evaluation_green_on_real_components(tmp_path: Path) -> None:
    evidence = drive_all(tmp_path)
    outcome = run_evaluation(tmp_path, evidence)
    assert outcome.result.passed is True
    assert outcome.mismatches == ()
    assert (evidence / RESULT_NAME).is_file()


def test_evaluation_green_tty_fixture_mode(tmp_path: Path) -> None:
    evidence = drive_all(tmp_path, tty_fixture=True)
    outcome = run_evaluation(tmp_path, evidence)
    assert outcome.result.passed is True


def test_tampered_object_fails_despite_valid_observation(tmp_path: Path) -> None:
    evidence = drive_all(tmp_path)
    observation = json.loads(
        (evidence / "scenarios" / "cp-atomic-publish" / "observation.json").read_text()
    )
    object_file = (
        Path(observation["work_dir"])
        / "store"
        / "objects"
        / observation["fields"]["reopen_digest"][:2]
        / observation["fields"]["reopen_digest"]
    )
    object_file.write_bytes(b"silently-tampered-after-drive")
    outcome = run_evaluation(tmp_path, evidence)
    assert outcome.result.passed is False
    assert any(row.code == "object-bytes-drift" for row in outcome.mismatches)


def test_fabricated_success_observation_fails(tmp_path: Path) -> None:
    evidence = drive_all(tmp_path)
    observation_path = evidence / "approvals" / "observation.json"
    payload = json.loads(observation_path.read_text())
    for operation in payload["operations"]:
        if operation["name"] == "automation-ingress-refused":
            operation["result"] = "ok"
    observation_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    outcome = run_evaluation(tmp_path, evidence)
    assert outcome.result.passed is False
    assert any(
        row.code == "operation-outcome-mismatch" for row in outcome.mismatches
    )


def test_superseded_authorization_claim_fails_evaluation(tmp_path: Path) -> None:
    evidence = drive_all(tmp_path)
    observation_path = evidence / "approvals" / "observation.json"
    payload = json.loads(observation_path.read_text())
    for operation in payload["operations"]:
        if operation["name"] == "superseded-record-no-longer-authorizes":
            operation["result"] = "authorized"
    observation_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    outcome = run_evaluation(tmp_path, evidence)
    assert outcome.result.passed is False


def test_parent_binding_drift_fails(tmp_path: Path) -> None:
    evidence = drive_all(tmp_path)
    outcome = run_evaluation(tmp_path, evidence, parent_sha256="5" * 64)
    assert outcome.result.passed is False
    assert any(row.code == "parent-gate-drift" for row in outcome.mismatches)


def test_manifest_binding_drift_fails(tmp_path: Path) -> None:
    evidence = drive_all(tmp_path)
    outcome = run_evaluation(tmp_path, evidence, fixture_manifest_sha256="6" * 64)
    assert outcome.result.passed is False
    assert any(row.code == "fixture-manifest-drift" for row in outcome.mismatches)


def test_missing_observation_fails(tmp_path: Path) -> None:
    evidence = drive_all(tmp_path)
    (evidence / "scenarios" / "cp-lease-expiry" / "observation.json").unlink()
    outcome = run_evaluation(tmp_path, evidence)
    assert outcome.result.passed is False
    assert any(row.code == "observation-missing" for row in outcome.mismatches)
