"""GateResult assembly and the H1 stop marker (Phase-1 gate).

``build_result`` folds the recomputed criterion state into the canonical
``GateResult`` over a full evidence-tree inventory; ``h1_marker`` assembles
and writes the ``h1-waiting`` stop record (next_checkpoint=H1,
status=NEEDS_HUMAN) bound to the execution work id and every fixture's
bundle/preview hashes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates import GatePolicy, GateResult
from services.gates.phase1_technical import PHASE_1_TECHNICAL_CRITERIA, PHASE_1_TECHNICAL_FIXTURES
from services.job_runner.gate_p1_models import (
    RESULT_NAME,
    FixtureH1Binding,
    FixtureObservation,
    H1Waiting,
)

if TYPE_CHECKING:
    from services.job_runner.gate_p1_checks import CheckState


class ResultAssemblyError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def h1_bindings(observations: dict[str, FixtureObservation]) -> tuple[FixtureH1Binding, ...]:
    bindings = []
    for fixture_id in PHASE_1_TECHNICAL_FIXTURES:
        observation = observations[fixture_id]
        run1 = Path(observation.run1_dir)
        document = json.loads((run1 / "review-bundle.json").read_bytes())
        if not isinstance(document, dict):
            raise ResultAssemblyError("bundle-malformed", str(run1))
        initial = document["initial"]
        current = document["current"]
        if not isinstance(initial, dict) or not isinstance(current, dict):
            raise ResultAssemblyError("bundle-malformed", str(run1))
        bindings.append(
            FixtureH1Binding(
                fixture_id=fixture_id,
                bundle_path=str(run1 / "review-bundle.json"),
                initial_plan_sha256=str(initial["plan_sha256"]),
                final_plan_sha256=str(current["plan_sha256"]),
                initial_preview_sha256=str(initial["preview_sha256"]),
                final_preview_sha256=str(current["preview_sha256"]),
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
                for criterion in PHASE_1_TECHNICAL_CRITERIA
            ],
        }
    )


def h1_marker(
    evidence: Path, policy_sha256: str, work_id: str, observations: dict[str, FixtureObservation]
) -> Path:
    waiting = H1Waiting(
        schema_version="h1-waiting-v1",
        gate_id="phase-1-technical",
        gate_version="v1",
        policy_sha256=policy_sha256,
        execution_work_id=work_id,
        fixtures=h1_bindings(observations),
    )
    target = evidence / "h1-waiting.json"
    atomic_write(target, canonical_model_bytes(waiting))
    return target


__all__ = ["ResultAssemblyError", "build_result", "h1_bindings", "h1_marker"]
