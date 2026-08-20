"""GateResult assembly for the Phase-3 gate (Todo 62)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import atomic_write, sha256_file
from services.gates import GatePolicy, GateResult
from services.gates.phase3 import PHASE_3_CRITERIA
from services.gates.serialization import canonical_gate_bytes

if TYPE_CHECKING:
    from services.job_runner.gate_p3_checks import CheckState

RESULT_NAME = "gate-result.json"


def build_result(
    policy: GatePolicy, policy_sha256: str, evidence: Path, state: CheckState
) -> GateResult:
    inventory: dict[str, str] = {}
    if evidence.is_dir():
        for path in sorted(evidence.rglob("*")):
            if path.is_file() and path.name != RESULT_NAME:
                inventory[path.relative_to(evidence).as_posix()] = sha256_file(path)
    payload = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    fallback = ["0" * 64]
    return GateResult.model_validate(
        {
            "schema_version": "gate-result-v1",
            "record_type": "gate_result",
            "gate_id": policy.gate_id,
            "gate_version": policy.gate_version,
            "policy_sha256": policy_sha256,
            "passed": all(state.criteria.values()),
            "evidence_bundles": [
                {
                    "bundle_sha256": hashlib.sha256(payload).hexdigest(),
                    "raw_evidence_sha256s": list(inventory.values()) or fallback,
                }
            ],
            "criteria_results": [
                {
                    "criterion_id": criterion,
                    "passed": state.criteria[criterion],
                    "raw_evidence_sha256s": state.evidence[criterion] or fallback,
                }
                for criterion in PHASE_3_CRITERIA
            ],
        }
    )


def write_result(evidence: Path, result: GateResult) -> Path:
    target = evidence / RESULT_NAME
    atomic_write(target, canonical_gate_bytes(result))
    return target


__all__ = ["RESULT_NAME", "build_result", "write_result"]
