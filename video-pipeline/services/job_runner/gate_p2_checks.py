"""Criterion recomputation for the Phase-2 Finalization Gate (Todo 54).

Recomputed from raw evidence only — compiled package bytes, fault-probe
records, interruption/drift/sweep records, build outputs, render files,
QC reports, final-review bundles/ledgers/records, frozen manifests, the
golden tables, the toolchain lock, the three parent gate results, and the
re-verified H1 prerequisite checkpoint. Nothing trusts the driver's
``observed_route``; every route is re-derived here from the artifacts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.foundation_io import canonical_model_bytes, sha256_file
from services.gates import GateResult
from services.gates.phase2 import PHASE_2_CRITERIA, PHASE_2_FIXTURES
from services.gates.prerequisite import PrerequisiteError, checkpoint_binding
from services.resolve_adapter.models import ResolvePackage

if TYPE_CHECKING:
    from services.fixtures.models import Phase2FreezeReceipt
    from services.gates import GatePolicy
    from services.job_runner.gate_p2_models import P2FixtureObservation

from services.job_runner.gate_p2_checks_media import check_conformance, check_qc, check_render
from services.job_runner.gate_p2_ir import manifest_for
from services.job_runner.gate_p2_routes import check_recovery

C_COMPILE, C_CONFORM, C_RENDER, C_QC, C_RECOVERY = PHASE_2_CRITERIA
MANIFEST_DIR: Final = Path("tests/fixtures/manifests/phase-2")
GOLDENS_DIR: Final = Path("tests/goldens/reference/phase-2")
LOCK_PATH: Final = Path("config/toolchains/phase-2-v2.json")
PARENT_GATES: Final = ("phase-0a", "phase-0b", "phase-1-technical")


@dataclass(frozen=True, slots=True)
class Mismatch:
    code: str
    detail: str


@dataclass(slots=True)
class CheckState:
    criteria: dict[str, bool] = field(
        default_factory=lambda: dict.fromkeys(PHASE_2_CRITERIA, True)
    )
    evidence: dict[str, list[str]] = field(
        default_factory=lambda: {key: [] for key in PHASE_2_CRITERIA}
    )
    mismatches: list[Mismatch] = field(default_factory=list)

    def fail(self, criterion: str, code: str, detail: str) -> None:
        self.criteria[criterion] = False
        self.mismatches.append(Mismatch(code=code, detail=detail))

    def note(self, criterion: str, *shas: str) -> None:
        for sha in shas:
            if sha and sha not in self.evidence[criterion]:
                self.evidence[criterion].append(sha)


def _load_json(path: Path) -> dict[str, object]:
    document = json.loads(path.read_bytes())
    if not isinstance(document, dict):
        raise TypeError(f"not a JSON object: {path}")
    return document


def check_bindings(
    policy: GatePolicy, receipt: Phase2FreezeReceipt | None, evidence: Path, state: CheckState
) -> str:
    """Verify every frozen binding; returns the re-verified checkpoint sha."""

    _check_frozen_inputs(policy, state)
    _check_parents(policy, evidence, state)
    return _check_prerequisite(policy, receipt, state)

def _check_frozen_inputs(policy: GatePolicy, state: CheckState) -> None:
    combined = hashlib.sha256()
    for fixture_id in PHASE_2_FIXTURES:
        combined.update((MANIFEST_DIR / f"{fixture_id}.json").read_bytes())
    if policy.fixture_manifest_sha256 != combined.hexdigest():
        state.fail(C_COMPILE, "fixture-manifest-drift", "manifest bytes drift from the policy")
    index_raw = (GOLDENS_DIR / "index.json").read_bytes()
    if policy.golden_sha256 != hashlib.sha256(index_raw).hexdigest():
        state.fail(C_COMPILE, "golden-index-drift", "golden index does not match the policy")
    index = _load_json(GOLDENS_DIR / "index.json")
    expected = (GOLDENS_DIR / "expected.json").read_bytes()
    if index["expected_sha256"] != hashlib.sha256(expected).hexdigest():
        state.fail(C_COMPILE, "golden-expected-drift", "golden tables do not match the index")
    state.note(C_COMPILE, hashlib.sha256(expected).hexdigest())
    if policy.toolchain_lock_sha256 != sha256_file(LOCK_PATH):
        state.fail(C_COMPILE, "toolchain-lock-drift", "toolchain lock bytes drift")


def _check_parents(policy: GatePolicy, evidence: Path, state: CheckState) -> None:
    parents = {
        gate: evidence.parent / gate / "gate-result.json"
        for gate in PARENT_GATES
        if (evidence.parent / gate / "gate-result.json").is_file()
    }
    if tuple(sorted(parents)) != PARENT_GATES or len(parents) != len(
        policy.parent_gate_result_hashes
    ):
        state.fail(C_COMPILE, "parent-gate-unbound", "the three parent gate results are missing")
        return
    for path in parents.values():
        raw = path.read_bytes()
        result = GateResult.model_validate_json(raw)
        if hashlib.sha256(raw).hexdigest() not in policy.parent_gate_result_hashes or not (
            result.passed
        ):
            state.fail(C_COMPILE, "parent-gate-drift", f"parent gate result drift: {path}")
    state.note(C_COMPILE, *[sha256_file(parents[gate]) for gate in PARENT_GATES])


def _check_prerequisite(
    policy: GatePolicy, receipt: Phase2FreezeReceipt | None, state: CheckState
) -> str:
    if receipt is None:
        state.fail(C_COMPILE, "freeze-receipt-missing", "the freeze receipt was not supplied")
        return ""
    checkpoint_path = Path(receipt.prerequisite_checkpoint_path)
    try:
        binding = checkpoint_binding(checkpoint_path)
    except (PrerequisiteError, OSError) as error:
        state.fail(C_COMPILE, "prerequisite-stale", f"H1 checkpoint no longer verifies: {error}")
        return ""
    checkpoint_sha = sha256_file(checkpoint_path)
    policy_binding = (
        policy.prerequisite_bindings[0] if len(policy.prerequisite_bindings) == 1 else None
    )
    if policy_binding is None or binding != policy_binding:
        state.fail(C_COMPILE, "prerequisite-binding-drift", "policy prerequisite binding mismatch")
    state.note(C_COMPILE, checkpoint_sha)
    return checkpoint_sha


def _golden_fixture(fixture_id: str) -> dict[str, object]:
    fixtures = _load_json(GOLDENS_DIR / "expected.json")["fixtures"]
    if not isinstance(fixtures, dict):
        raise TypeError("golden fixtures table is not an object")
    entry = fixtures[fixture_id]
    if not isinstance(entry, dict):
        raise TypeError(f"golden fixture entry malformed: {fixture_id}")
    return entry


def check_package(observation: P2FixtureObservation, state: CheckState) -> None:
    fixture = observation.fixture_id
    package_path = observation.fields.get("package") or observation.fields.get("live_package")
    if package_path is None:
        state.fail(C_COMPILE, "package-missing", f"{fixture}: no compiled package artifact")
        return
    raw = Path(package_path).read_bytes()
    package = ResolvePackage.model_validate_json(raw)
    zeroed = package.model_copy(update={"content_hash": "0" * 64})
    digest = hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()
    if digest != package.content_hash:
        state.fail(C_COMPILE, "package-hash-invalid", f"{fixture}: content hash chain broken")
    golden = _golden_fixture(fixture)
    golden_package = _load_json_path(golden["package"])
    placements = golden_package["placements"]
    if not isinstance(placements, list):
        raise TypeError("golden placements table is not a list")
    expected_rows = {
        (str(row["item_id"]), json.dumps(row["clip_info"], sort_keys=True))
        for row in placements
        if isinstance(row, dict)
    }
    observed_rows = {
        (
            p.item_id,
            json.dumps(json.loads(p.clip_info.model_dump_json()), sort_keys=True),
        )
        for p in package.placements
    }
    if observed_rows != expected_rows:
        state.fail(
            C_COMPILE, "package-golden-drift", f"{fixture}: placements drift from the golden table"
        )
    state.note(C_COMPILE, hashlib.sha256(raw).hexdigest())


def _load_json_path(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("golden package table is not an object")
    return value


def check_all(
    policy: GatePolicy,
    receipt: Phase2FreezeReceipt | None,
    evidence: Path,
    observations: dict[str, P2FixtureObservation],
) -> tuple[CheckState, str]:
    state = CheckState()
    checkpoint_sha = check_bindings(policy, receipt, evidence, state)
    for fixture_id in PHASE_2_FIXTURES:
        observation = observations[fixture_id]
        manifest = manifest_for(fixture_id)
        try:
            check_package(observation, state)
            check_conformance(observation, manifest, state)
            check_render(observation, manifest, state)
            check_qc(observation, manifest, state)
            check_recovery(observation, manifest, state)
        except (OSError, ValidationError, ValueError, TypeError, KeyError) as error:
            state.fail(C_RECOVERY, "evidence-malformed", f"{fixture_id}: {error}")
    for criterion in PHASE_2_CRITERIA:
        if not state.evidence[criterion]:
            state.fail(criterion, "evidence-incomplete", criterion)
    return state, checkpoint_sha


__all__ = [
    "C_COMPILE",
    "CheckState",
    "Mismatch",
    "check_all",
    "check_bindings",
    "check_package",
]
