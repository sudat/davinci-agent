"""Per-criterion recomputation checks for the Control Plane gate.

All checks derive pass/fail from raw evidence only: observation files
record operation outcomes, and every filesystem fact (object bytes,
temps, meta sidecars, reconcile logs, registry index) is recomputed
from the scenario scratch stores — an authored pass flag would have
nothing to forge against. Criterion mapping is documented in
``gate_cp_evaluate``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from services.artifact_registry.models import RegistryIndex
from services.artifact_store.reconcile import ReconcileReport
from services.contracts.serialization import canonical_json_bytes
from services.gates.control_plane import PHASE_1_CONTROL_PLANE_CRITERIA
from services.job_runner.gate_cp_models import GateObservation

CriterionId = str
ATOMIC = "phase-1-cp-atomic-publication"


@dataclass(frozen=True, slots=True)
class GateMismatch:
    code: str
    detail: str


@dataclass(slots=True)
class CheckState:
    criteria: dict[CriterionId, bool] = field(
        default_factory=lambda: dict.fromkeys(PHASE_1_CONTROL_PLANE_CRITERIA, True)
    )
    evidence: dict[CriterionId, list[str]] = field(
        default_factory=lambda: {
            criterion: [] for criterion in PHASE_1_CONTROL_PLANE_CRITERIA
        }
    )
    mismatches: list[GateMismatch] = field(default_factory=list)

    def fail(self, criterion: CriterionId, code: str, detail: str) -> None:
        self.criteria[criterion] = False
        self.mismatches.append(GateMismatch(code=code, detail=detail))

    def note(self, criterion: CriterionId, sha: str) -> None:
        if sha and sha not in self.evidence[criterion]:
            self.evidence[criterion].append(sha)

    def op(
        self, criterion: CriterionId, observation: GateObservation, name: str, expected: str
    ) -> None:
        self.note(criterion, observation_sha(observation))
        try:
            outcome = observation.operation(name)
        except KeyError:
            self.fail(criterion, "operation-missing", f"{observation.fixture_id}:{name}")
            return
        if outcome.result != expected:
            self.fail(
                criterion,
                "operation-outcome-mismatch",
                f"{observation.fixture_id}:{name} {outcome.result!r} != {expected!r}",
            )


def observation_sha(observation: GateObservation) -> str:
    return hashlib.sha256(canonical_json_bytes(observation)).hexdigest()


def load_observation(path: Path) -> GateObservation | None:
    try:
        return GateObservation.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return None


def load_all_observations(evidence: Path) -> dict[str, GateObservation]:
    observations: dict[str, GateObservation] = {}
    for fixture_id in (
        "cp-atomic-publish",
        "cp-crash-before-rename",
        "cp-orphan-reconcile",
        "cp-stale-cas",
        "cp-lease-expiry",
        "cp-path-symlink-denial",
    ):
        loaded = load_observation(evidence / "scenarios" / fixture_id / "observation.json")
        if loaded is not None:
            observations[fixture_id] = loaded
    approvals = load_observation(evidence / "approvals" / "observation.json")
    if approvals is not None:
        observations["cp-approvals"] = approvals
    return observations


def object_digest(scenario_work: Path, sha: str) -> str | None:
    object_file = scenario_work / "store" / "objects" / sha[:2] / sha
    try:
        raw = object_file.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(raw).hexdigest()


def meta_present(scenario_work: Path, artifact_id: str) -> bool:
    return (scenario_work / "store" / "artifacts" / f"{artifact_id}.json").is_file()


def temp_present(scenario_work: Path, sha: str) -> bool:
    return (scenario_work / "store" / "objects" / sha[:2] / f".obj-{sha}.tmp").exists()


def reconcile_actions(scenario_work: Path) -> tuple[str, ...]:
    log = scenario_work / "store" / "reconcile-log.jsonl"
    try:
        lines = log.read_text().splitlines()
    except OSError:
        return ()
    actions = []
    for line in lines:
        if not line:
            continue
        actions.append(ReconcileReport.model_validate_json(line).entries[0].action)
    return tuple(actions)


def registry_has(scenario_work: Path, artifact_id: str) -> bool:
    index_file = scenario_work / "registry" / "registry-index.json"
    try:
        index = RegistryIndex.model_validate_json(index_file.read_bytes())
    except (OSError, ValidationError):
        return False
    return artifact_id in index.entries


__all__ = [
    "ATOMIC",
    "CheckState",
    "GateMismatch",
    "load_all_observations",
    "load_observation",
    "meta_present",
    "object_digest",
    "observation_sha",
    "reconcile_actions",
    "registry_has",
    "temp_present",
]
