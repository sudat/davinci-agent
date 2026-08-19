"""GateResult assembly and the truthful phase-2 exit marker (Todo 54).

``build_result`` folds the recomputed criterion state into the canonical
``GateResult`` over a full evidence-tree inventory. ``exit_marker``
assembles the ``phase-2-exit`` record: ``next_checkpoint=PHASE_2_EXIT``
with every fixture Final record labeled ``fixture_only`` — the gate never
presents fixture completions as publication decisions.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates import GatePolicy, GateResult
from services.gates.phase2 import PHASE_2_CRITERIA, PHASE_2_FIXTURES
from services.job_runner.gate_p2_models import (
    EXIT_MARKER_NAME,
    RESULT_NAME,
    FixtureExitBinding,
    Phase2ExitMarker,
)

if TYPE_CHECKING:
    from services.job_runner.gate_p2_checks import CheckState
    from services.job_runner.gate_p2_models import P2FixtureObservation


def exit_bindings(
    observations: dict[str, P2FixtureObservation]
) -> tuple[FixtureExitBinding, ...]:
    bindings = []
    for fixture_id in PHASE_2_FIXTURES:
        observation = observations[fixture_id]
        fields = observation.fields
        qc_verdict: str | None = None
        report_path = fields.get("qc_report")
        if report_path is not None and Path(report_path).is_file():
            document = json.loads(Path(report_path).read_bytes())
            verdict = document.get("verdict")
            qc_verdict = verdict if isinstance(verdict, str) else None
        bindings.append(
            FixtureExitBinding(
                fixture_id=fixture_id,
                fixture_record_fixture_only=True,
                bundle_path=fields.get("bundle_path"),
                render_sha256=fields.get("render_sha256"),
                qc_verdict=qc_verdict,
                build_output_path=fields.get("build_output"),
                approval_path=fields.get("approval_path"),
                approval_refusal_path=fields.get("approval_refusal_path"),
                human_route_path=fields.get("human_route"),
            )
        )
    return tuple(bindings)


def build_result(
    policy: GatePolicy,
    policy_sha256: str,
    evidence: Path,
    state: CheckState,
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
                for criterion in PHASE_2_CRITERIA
            ],
        }
    )


def exit_marker(
    evidence: Path, policy_sha256: str, observations: dict[str, P2FixtureObservation]
) -> Path:
    marker = Phase2ExitMarker(
        schema_version="phase-2-exit-v1",
        gate_id="phase-2",
        gate_version="v1",
        policy_sha256=policy_sha256,
        fixtures=exit_bindings(observations),
    )
    target = evidence / EXIT_MARKER_NAME
    atomic_write(target, canonical_model_bytes(marker))
    return target


__all__ = ["build_result", "exit_bindings", "exit_marker"]
