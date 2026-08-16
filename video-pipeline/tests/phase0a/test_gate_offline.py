from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.foundation_io import sha256_file
from services.gates import GatePolicy, GateResult
from services.resolve_bridge.build_report_fakes import FakeBuildTools
from services.spike.gate_evaluate import GateEvaluationInputs, GateOutcome, evaluate, write_result
from services.spike.gate_fakes import HOST_NAME, FaultKnobs, synthesize
from services.spike.gate_models import CapabilityEntry, CapabilityMatrix, EvidenceRef
from services.spike.stop_rules import (
    STOP_CAPABILITY,
    STOP_IDENTITY,
    capability_stop_reason,
    identity_stop_reason,
    load_stop,
    record_stop,
    stop_marker_path,
    stop_recorded,
)

MANIFEST = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")
POLICY = Path("config/gates/phase-0a-v1.json")
FAULTS = Path("tests/fixtures/resolve-bridge-faults")


def _policy() -> GatePolicy:
    return GatePolicy.model_validate_json(POLICY.read_bytes())


def _evaluate(evidence: Path, fixture: Path) -> tuple[GateOutcome, Path]:
    outcome = evaluate(
        GateEvaluationInputs(
            policy=_policy(),
            policy_sha256=sha256_file(POLICY),
            evidence=evidence,
            manifest_path=MANIFEST,
            host_report_path=evidence / HOST_NAME,
            fixture_dir=fixture,
            ffmpeg_bin=evidence / "ffmpeg",
            ffprobe_bin=evidence / "ffprobe",
            port=FakeBuildTools(),
        )
    )
    return outcome, write_result(outcome, evidence)


def _synthesize(tmp_path: Path, knobs: FaultKnobs) -> tuple[Path, Path]:
    evidence = tmp_path / "phase-0a"
    fixture = synthesize(MANIFEST, evidence, knobs)
    return evidence, fixture


def test_fake_matrix_passes_all_criteria(tmp_path: Path) -> None:
    evidence, fixture = _synthesize(tmp_path, FaultKnobs())
    outcome, result_path = _evaluate(evidence, fixture)
    assert outcome.result.passed, [row.code for row in outcome.mismatches]
    assert outcome.stop_triggered is False
    assert len(outcome.run_fingerprints) == 6
    assert len(set(outcome.run_fingerprints)) == 1
    assert outcome.expected_fingerprint == outcome.run_fingerprints[0]
    raw = result_path.read_bytes()
    result = GateResult.model_validate_json(raw)
    assert result.passed is True
    assert result.policy_sha256 == sha256_file(POLICY)
    assert tuple(row.criterion_id for row in result.criteria_results) == tuple(
        _policy().criteria
    )


def test_stale_prior_build_evidence_cannot_pass(tmp_path: Path) -> None:
    evidence, fixture = _synthesize(tmp_path, FaultKnobs(stale_prior_build=True))
    outcome, _ = _evaluate(evidence, fixture)
    assert outcome.result.passed is False
    assert outcome.stop_triggered is False
    codes = {row.code for row in outcome.mismatches}
    assert "stale-prior-build-evidence" in codes


def test_restart_drift_cannot_pass(tmp_path: Path) -> None:
    evidence, fixture = _synthesize(tmp_path, FaultKnobs(restart_drift=True))
    outcome, _ = _evaluate(evidence, fixture)
    assert outcome.result.passed is False
    assert {row.code for row in outcome.mismatches} == {"restart-binding-drift"}


def test_partial_timeline_dependence_cannot_pass(tmp_path: Path) -> None:
    evidence, fixture = _synthesize(tmp_path, FaultKnobs(partial_dependence=True))
    outcome, _ = _evaluate(evidence, fixture)
    assert outcome.result.passed is False
    assert "partial-timeline-dependence" in {row.code for row in outcome.mismatches}


def test_stop_source_identity_triggers_stop(tmp_path: Path) -> None:
    evidence, fixture = _synthesize(tmp_path, FaultKnobs(stop_source_identity=True))
    outcome, _ = _evaluate(evidence, fixture)
    assert outcome.stop_triggered is True
    assert outcome.stop_criterion == STOP_IDENTITY
    assert outcome.result.passed is False


def test_stop_capability_missing_triggers_stop(tmp_path: Path) -> None:
    evidence, fixture = _synthesize(tmp_path, FaultKnobs(stop_capability_missing=True))
    outcome, _ = _evaluate(evidence, fixture)
    assert outcome.stop_triggered is True
    assert outcome.stop_criterion == STOP_CAPABILITY
    assert "render" in outcome.stop_reason


def test_missing_run_evidence_cannot_pass(tmp_path: Path) -> None:
    evidence, fixture = _synthesize(tmp_path, FaultKnobs())
    (evidence / "runs" / "run-5" / "build-report.json").unlink()
    outcome, _ = _evaluate(evidence, fixture)
    assert outcome.result.passed is False
    codes = {row.code for row in outcome.mismatches}
    assert "missing-run-evidence" in codes


def test_gate_result_rejects_float_payload() -> None:
    payload = {
        "schema_version": "gate-result-v1",
        "record_type": "gate_result",
        "gate_id": "phase-0a",
        "gate_version": "v1",
        "policy_sha256": "a" * 64,
        "passed": True,
        "evidence_bundles": [{"bundle_sha256": "b" * 64, "raw_evidence_sha256s": ["c" * 64]}],
        "criteria_results": [
            {
                "criterion_id": "phase-0a-frame-delta-zero",
                "passed": True,
                "raw_evidence_sha256s": ["d" * 64],
            }
        ],
    }
    payload["evidence_bundles"][0]["raw_evidence_sha256s"] = ["c" * 64]
    with_valid = GateResult.model_validate_json(json.dumps(payload))
    assert with_valid.passed is True
    payload["criteria_results"][0]["raw_evidence_sha256s"] = ["d" * 63]
    with pytest.raises(ValidationError):
        GateResult.model_validate_json(json.dumps(payload))


def test_identity_stop_reason_cases() -> None:
    digest = "a" * 64
    assert identity_stop_reason(None, (), digest) is not None
    assert identity_stop_reason(digest, (digest,), None) is not None
    reason = identity_stop_reason(digest, (digest, "b" * 64), digest)
    assert reason is not None
    assert "run[2]" in reason
    assert identity_stop_reason(digest, (digest, digest), digest) is None


def test_capability_stop_reason_cases(tmp_path: Path) -> None:
    ref = EvidenceRef(path="runs/run-1/build-report.json", sha256="a" * 64)
    entry = CapabilityEntry(
        capability="base_cut",
        api_available=True,
        live_verified=True,
        evidence_refs=(ref,),
        limitations="unit",
    )
    matrix = CapabilityMatrix(
        schema_version="capability-matrix-v1",
        resolve_version="21.0.4",
        resolve_build="21.0.4",
        capabilities=(entry,),
        findings=(),
    )
    assert capability_stop_reason(None, tmp_path) is not None
    assert capability_stop_reason(matrix, tmp_path) is not None  # refs do not hash-match
    render_entry = entry.model_copy(update={"capability": "render"})
    without_base = matrix.model_copy(update={"capabilities": (render_entry,)})
    reason = capability_stop_reason(without_base, None)
    assert reason is not None
    assert "base_cut" in reason
    unverified = CapabilityEntry(
        capability="base_cut",
        api_available=False,
        live_verified=False,
        evidence_refs=(ref,),
        limitations="unit",
    )
    assert capability_stop_reason(
        matrix.model_copy(update={"capabilities": (unverified,)}), None
    ) is not None


def test_stop_marker_blocks_rerun(tmp_path: Path) -> None:
    evidence = tmp_path / "phase-0b"
    evidence.mkdir()
    assert stop_recorded(evidence) is None
    marker = record_stop(evidence, STOP_IDENTITY, "unit test stop")
    assert marker == stop_marker_path(evidence)
    recorded = stop_recorded(evidence)
    assert recorded is not None
    assert recorded.criterion_id == STOP_IDENTITY
    assert load_stop(marker).gate_id == "phase-0a"


@pytest.mark.parametrize(
    ("fixture", "observation"),
    [
        ("gate-stale-prior-build-evidence.json", "code=stale-prior-build-evidence"),
        ("gate-restart-drift.json", "code=restart-binding-drift"),
        ("gate-partial-timeline-dependence.json", "code=partial-timeline-dependence"),
        (
            "gate-stop-source-identity.json",
            "STOP code=phase-0a-stop-source-identity-or-frame-unverifiable",
        ),
        ("gate-stop-capability-missing.json", "STOP code=phase-0a-stop-mvp-capability-unavailable"),
    ],
)
def test_cli_fault_mode_detects_injected_faults(fixture: str, observation: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.spike.run_gate",
            "phase-0a",
            "--policy",
            str(POLICY),
            "--manifest",
            str(MANIFEST),
        ],
        env=os.environ | {"QA_FAULT_FIXTURE": str(FAULTS / fixture)},
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    assert observation in combined, combined
    assert "ERROR:" not in combined


def test_cli_refuses_to_run_when_stop_recorded(tmp_path: Path) -> None:
    evidence = tmp_path / "phase-0a"
    evidence.mkdir()
    record_stop(evidence, STOP_IDENTITY, "recorded stop blocks reruns")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.spike.run_gate",
            "phase-0a",
            "--policy",
            str(POLICY),
            "--evidence",
            str(evidence),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 3, combined
    assert "STOP-REFUSED" in combined
    assert "recorded stop blocks reruns" in combined


def test_cli_rejects_invalid_and_noncanonical_policy(tmp_path: Path) -> None:
    evidence = tmp_path / "phase-0a"
    evidence.mkdir()
    invalid = tmp_path / "invalid-policy.json"
    invalid.write_text("{not-json")
    for policy_path, expected in ((invalid, "policy-invalid"), (POLICY, "policy")):
        if policy_path == POLICY:
            continue
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "services.spike.run_gate",
                "phase-0a",
                "--policy",
                str(policy_path),
                "--evidence",
                str(evidence),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 1, combined
        assert expected in combined
    pretty = tmp_path / "pretty-policy.json"
    pretty.write_text(json.dumps(json.loads(POLICY.read_text()), indent=2))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.spike.run_gate",
            "phase-0a",
            "--policy",
            str(pretty),
            "--evidence",
            str(evidence),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1
    assert "policy-noncanonical" in result.stdout + result.stderr


def test_cli_requires_known_gate_name() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "services.spike.run_gate", "phase-9z", "--policy", str(POLICY)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2
