"""Synthetic Phase-2 regression result for the offline Phase-3 harness.

Lives beside the live regression module but owns only the OFFLINE
synthesis: the fake tree cannot re-run the live Phase-2 gate, so it
materializes a regression result through the same strict models the live
gate writes. The bytes are clearly synthetic (``SYNTHETIC_VERSION``) while
still binding the real pinned Phase-2 policy hash and the real Phase-2
criteria.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from services.foundation_io import atomic_write
from services.gates import GateResult
from services.gates.phase2 import PHASE_2_CRITERIA
from services.gates.serialization import canonical_gate_bytes
from services.job_runner.gate_p3_models import (
    REGRESSION_DIR_NAME,
    SYNTHETIC_VERSION,
    P3RegressionObservation,
)
from services.job_runner.gate_p3_regression import phase2_policy, repo_root


def synthetic_policy_sha256() -> str:
    """sha256 of the pinned phase-2 policy the synthetic result binds."""
    return hashlib.sha256((repo_root() / phase2_policy()).read_bytes()).hexdigest()


def synth_regression(evidence: Path) -> P3RegressionObservation:
    """Synthesize the phase-2 regression rerun result and its observation."""
    policy_sha = synthetic_policy_sha256()
    bundle = hashlib.sha256(b"synthetic-phase2").hexdigest()
    result = GateResult.model_validate(
        {
            "schema_version": "gate-result-v1",
            "record_type": "gate_result",
            "gate_id": "phase-2",
            "gate_version": SYNTHETIC_VERSION,
            "policy_sha256": policy_sha,
            "passed": True,
            "evidence_bundles": [
                {"bundle_sha256": bundle, "raw_evidence_sha256s": [bundle]}
            ],
            "criteria_results": [
                {
                    "criterion_id": criterion,
                    "passed": True,
                    "raw_evidence_sha256s": [hashlib.sha256(criterion.encode()).hexdigest()],
                }
                for criterion in PHASE_2_CRITERIA
            ],
        }
    )
    reg_dir = evidence / REGRESSION_DIR_NAME
    reg_dir.mkdir(parents=True, exist_ok=True)
    raw = canonical_gate_bytes(result)
    atomic_write(reg_dir / "gate-result.json", raw)
    return P3RegressionObservation(
        evidence_dir=str(reg_dir),
        exit_code=0,
        timed_out=False,
        result_path=str(reg_dir / "gate-result.json"),
        result_sha256=hashlib.sha256(raw).hexdigest(),
        policy_sha256=policy_sha,
        criteria_passed=dict.fromkeys(PHASE_2_CRITERIA, True),
        passed=True,
    )


__all__ = ["synth_regression", "synthetic_policy_sha256"]
