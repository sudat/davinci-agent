"""Phase-3 fault-harness acceptance: the ``QA_FAULT_FIXTURE`` CLI surface.

Every fault fixture runs through the real run_gate CLI and the in-process
``run_fault_cli``; each must print synthetic provenance, pass the baseline,
and detect exactly the expected typed code. Context trust-boundary behavior
lives in ``test_gate_synthetic_context.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from services.job_runner.gate_p3_faults import run_fault_cli

POLICY = Path("config/gates/phase-3-v3.json")
FAULT_DIR = Path("tests/fixtures/phase3-gate-faults")
FAULT_CASES = (
    ("editorial_structure_drift", "ab-structure-drift"),
    ("one_profile_only", "one-profile-only-success"),
    ("channel_branch", "channel-branch-present"),
    ("stale_asset", "stale-asset"),
    ("fallback_to_manual", "fallback-to-manual"),
    ("rights_issue", "rights-issue"),
    ("phase4_import", "phase4-import-attempted"),
)


def test_fault_cli_detects_injected_fault(tmp_path: Path) -> None:
    argv = (
        sys.executable,
        "-m",
        "services.job_runner.run_gate",
        "phase-3",
        "--policy",
        str(POLICY),
        "--evidence",
        str(tmp_path / "fault-evidence"),
    )
    result = subprocess.run(
        argv,
        env={
            "QA_FAULT_FIXTURE": str(FAULT_DIR / "stale_asset.json"),
            "PATH": "/usr/bin:/bin",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "detected=true expected_code=stale-asset" in result.stdout
    assert "baseline=PASS" in result.stdout


def test_fault_cli_refuses_unknown_fault(tmp_path: Path) -> None:
    argv = (
        sys.executable,
        "-m",
        "services.job_runner.run_gate",
        "phase-3",
        "--policy",
        str(POLICY),
        "--evidence",
        str(tmp_path / "fault-evidence"),
    )
    result = subprocess.run(
        argv,
        env={"QA_FAULT_FIXTURE": "/nonexistent/fault.json", "PATH": "/usr/bin:/bin"},
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2
    assert "fault-unreadable" in result.stderr


def test_real_cli_prints_synthetic_provenance(tmp_path: Path) -> None:
    argv = (
        sys.executable,
        "-m",
        "services.job_runner.run_gate",
        "phase-3",
        "--policy",
        str(POLICY),
        "--evidence",
        str(tmp_path / "fault-evidence"),
    )
    result = subprocess.run(
        argv,
        env={
            "QA_FAULT_FIXTURE": str(FAULT_DIR / "stale_asset.json"),
            "PATH": "/usr/bin:/bin",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "phase3-gate mode=synthetic synthetic_policy_sha256=" in result.stdout


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
    assert "phase3-gate mode=synthetic synthetic_policy_sha256=" in captured.out
    assert "baseline=PASS" in captured.out
    assert f"detected=true expected_code={expected_code}" in captured.out
