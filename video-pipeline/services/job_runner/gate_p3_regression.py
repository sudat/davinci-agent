"""Phase-2 regression rerun for the Phase-3 gate (Todo 62).

The COMPLETE Phase-2 gate re-executes live (offline fault probes plus the
bridge builds) into its own evidence directory under the Phase-3 tree —
the frozen ``$ATTEMPT_DIR/phase-2`` parent result is never touched. The
rerun is bounded by an explicit timeout; every outcome (nonzero exit,
timeout, missing result, any criterion false, policy binding drift) is a
typed regression failure.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

from services.gates import GateResult
from services.job_runner.gate_p3_models import (
    REGRESSION_DIR_NAME,
    P3RegressionObservation,
)

MARKER: Final = "phase3-gate"
PHASE2_POLICY: Final = Path("config/gates/phase-2-v1.json")
PHASE2_RECEIPT_NAME: Final = "task-47-freeze-receipt.json"
ATTEMPT: Final = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
DEFAULT_TIMEOUT_SECONDS: Final = 5400.0


def default_receipt(evidence: Path) -> Path:
    """The frozen phase-2 freeze receipt beside the attempt root."""

    for base in (evidence.parent, ATTEMPT):
        candidate = base / PHASE2_RECEIPT_NAME
        if candidate.is_file():
            return candidate
    return ATTEMPT / PHASE2_RECEIPT_NAME


PARENT_GATES: Final = ("phase-0a", "phase-0b", "phase-1-technical")


def _link_phase2_parents(evidence: Path) -> None:
    """Copy the frozen phase-2 parent results beside the regression tree.

    The phase-2 gate binds its three parent results relative to its own
    evidence directory; the copies come from the frozen attempt artifacts
    and the originals are never touched.
    """

    for gate in PARENT_GATES:
        target_dir = evidence / gate
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            ATTEMPT / gate / "gate-result.json", target_dir / "gate-result.json"
        )


def run_regression(
    evidence: Path,
    *,
    host_report: Path,
    receipt: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> P3RegressionObservation:
    """Re-run the frozen Phase-2 gate; parse its result honestly."""

    regression_dir = evidence / REGRESSION_DIR_NAME
    _link_phase2_parents(evidence)
    repo = repo_root()
    policy_path = repo / PHASE2_POLICY
    argv = (
        sys.executable,
        "-m",
        "services.job_runner.run_gate",
        "phase-2",
        "--policy",
        str(policy_path),
        "--evidence",
        str(regression_dir),
        "--freeze-receipt",
        str(receipt),
    )
    environment = dict(os.environ)
    environment["RESOLVE_HOST_REPORT"] = str(host_report)
    timed_out = False
    exit_code = 0
    try:
        completed = subprocess.run(
            argv,
            cwd=repo,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        exit_code = completed.returncode
        tail = (completed.stdout + completed.stderr).strip().splitlines()[-4:]
        for line in tail:
            print(f"{MARKER} regression: {line}")
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = -1
        print(f"{MARKER} regression: TIMEOUT after {timeout_seconds}s", file=sys.stderr)
    result_path = regression_dir / "gate-result.json"
    passed = False
    criteria: dict[str, bool] = {}
    result_sha = "0" * 64
    policy_sha = "0" * 64
    if result_path.is_file():
        raw = result_path.read_bytes()
        result_sha = hashlib.sha256(raw).hexdigest()
        try:
            parsed = GateResult.model_validate_json(raw)
        except ValueError:
            passed = False
        else:
            passed = parsed.passed
            criteria = {
                row.criterion_id: row.passed for row in parsed.criteria_results
            }
            policy_sha = parsed.policy_sha256
    return P3RegressionObservation(
        evidence_dir=str(regression_dir),
        exit_code=exit_code,
        timed_out=timed_out,
        result_path=str(result_path),
        result_sha256=result_sha,
        policy_sha256=policy_sha,
        criteria_passed=criteria,
        passed=passed,
    )


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def host_report_path(evidence: Path) -> Path | None:
    override = os.environ.get("RESOLVE_HOST_REPORT")
    candidates = [Path(override)] if override else []
    fallback = evidence.parent / "resolve-host.json"
    candidates.append(fallback)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MARKER",
    "PHASE2_POLICY",
    "host_report_path",
    "run_regression",
]
