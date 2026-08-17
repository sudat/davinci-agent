"""Recompute the five Control Plane criteria from raw evidence.

Never trusts an authored pass flag: observations carry raw operation
outcomes only, and every filesystem-derived fact (object bytes, temps,
meta sidecars, reconcile logs, registry index) is recomputed by the
check layer from the scenario scratch stores. Criterion mapping (fixed
by the frozen policy's five criteria):

- ``phase-1-cp-atomic-publication``: cp-atomic-publish plus
  cp-path-symlink-denial, approval purpose/target separation and
  fixture-vs-operator labeling.
- ``phase-1-cp-crash-reconciliation``: cp-crash-before-rename plus
  cp-orphan-reconcile (temp discard + registry adoption).
- ``phase-1-cp-lease-authority``: cp-lease-expiry (flock authority and
  Todo-10 SQL lanes) plus TTY-only operator ingress refusals.
- ``phase-1-cp-stale-cas-suppression``: cp-stale-cas (reopen rejection
  and State-vs-Artifact fail-closed) plus superseded records no longer
  authorizing and the losing serialized writer suppressed.
- ``phase-1-cp-idempotent-resume``: idempotent republish after the
  injected crash plus idempotent CAS replay.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.foundation_io import atomic_write, sha256_file
from services.gates import GatePolicy, GateResult
from services.gates.control_plane import PHASE_1_CONTROL_PLANE_CRITERIA
from services.gates.serialization import canonical_gate_bytes
from services.job_runner.gate_cp_checks import (
    ATOMIC,
    CheckState,
    GateMismatch,
    load_all_observations,
)
from services.job_runner.gate_cp_criteria import (
    check_atomic,
    check_bindings,
    check_crash,
    check_lease,
    check_resume,
    check_stale,
)
from services.job_runner.gate_cp_scenarios import load_manifests

RESULT_NAME: Final = "gate-result.json"
RESULT_SCHEMA: Final = "gate-result-v1"


@dataclass(frozen=True, slots=True)
class ControlPlaneOutcome:
    result: GateResult
    mismatches: tuple[GateMismatch, ...]
    result_path: Path


def evaluate_gate(  # noqa: PLR0913 (gate contract: policy + bindings + evidence root)
    policy: GatePolicy,
    *,
    policy_sha256: str,
    evidence: Path,
    manifest_dir: Path,
    toolchain_lock: Path,
    parent_result: Path,
) -> ControlPlaneOutcome:
    state = CheckState()
    manifests = load_manifests(manifest_dir)
    for fixture_id in manifests:
        state.note(ATOMIC, sha256_file(manifest_dir / f"{fixture_id}.json"))
    check_bindings(policy, manifest_dir, toolchain_lock, parent_result, state)

    observations = load_all_observations(evidence)
    check_atomic(
        manifests["cp-atomic-publish"], manifests["cp-path-symlink-denial"], observations, state
    )
    check_crash(
        manifests["cp-crash-before-rename"],
        manifests["cp-orphan-reconcile"],
        observations,
        state,
    )
    check_lease(observations, state)
    check_stale(manifests["cp-stale-cas"], observations, state)
    check_resume(manifests["cp-crash-before-rename"], observations, state)
    return _build_result(policy, policy_sha256, evidence, state)


def _build_result(
    policy: GatePolicy,
    policy_sha256: str,
    evidence: Path,
    state: CheckState,
) -> ControlPlaneOutcome:
    criteria_payload = [
        {
            "criterion_id": criterion,
            "passed": state.criteria[criterion],
            "raw_evidence_sha256s": list(state.evidence[criterion]) or ["0" * 64],
        }
        for criterion in PHASE_1_CONTROL_PLANE_CRITERIA
    ]
    inventory = _evidence_inventory(evidence)
    payload_json = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    fallback = state.evidence[PHASE_1_CONTROL_PLANE_CRITERIA[0]] or ["0" * 64]
    bundle_raw = tuple(inventory.values()) or tuple(fallback)
    result = GateResult.model_validate(
        {
            "schema_version": RESULT_SCHEMA,
            "record_type": "gate_result",
            "gate_id": policy.gate_id,
            "gate_version": policy.gate_version,
            "policy_sha256": policy_sha256,
            "passed": all(state.criteria.values()),
            "evidence_bundles": [
                {
                    "bundle_sha256": hashlib.sha256(payload_json).hexdigest(),
                    "raw_evidence_sha256s": list(bundle_raw),
                }
            ],
            "criteria_results": criteria_payload,
        }
    )
    result_path = evidence / RESULT_NAME
    atomic_write(result_path, canonical_gate_bytes(result))
    return ControlPlaneOutcome(
        result=result, mismatches=tuple(state.mismatches), result_path=result_path
    )


def _evidence_inventory(evidence: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    if evidence.is_dir():
        for path in sorted(evidence.rglob("*")):
            if path.is_file() and path.name != RESULT_NAME:
                inventory[path.relative_to(evidence).as_posix()] = sha256_file(path)
    return inventory


__all__ = ["RESULT_NAME", "RESULT_SCHEMA", "ControlPlaneOutcome", "evaluate_gate"]
