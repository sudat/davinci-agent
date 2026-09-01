"""Offline fault scenarios for the Phase-1 Technical Gate (``QA_FAULT_FIXTURE``).

Synthesizes the full five-fixture evidence tree from the frozen goldens with
one injected defect each (no chain run, no ffmpeg, no Resolve), then runs the
REAL evaluator with driving disabled. The baseline no-fault synthesis must
PASS every criterion (printed as ``baseline=PASS``) — a fault is ``detected``
only when the gate FAILS and the recomputed mismatches carry the expected
code. Exit 0 = detected, 2 = missed/unknown fault.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter, ValidationError

from services.foundation_io import sha256_file
from services.gates import (
    CriterionResult,
    EvidenceBundleRef,
    GatePolicy,
    GateResult,
    canonical_gate_bytes,
)
from services.gates.models import InputValue
from services.job_runner import gate_phase1
from services.job_runner.gate_p1_fake_tree import synthesize
from services.job_runner.gate_p1_fakes import FAULTS, FaultKnobs

MARKER: Final = "phase1-gate"
EXIT_DETECTED: Final = 0
EXIT_FAULT: Final = 2
POLICY: Final = Path("config/gates/phase-1-technical-v1.json")
SYNTHETIC_VERSION: Final = "synthetic-fault-harness-v1"
_PHASE1_GATE_ID: Final = "phase-1-technical"
_INFRASTRUCTURE_MISMATCH_CODES: Final = frozenset(
    {
        "toolchain-lock-drift",
        "fixture-manifest-drift",
        "golden-index-drift",
        "golden-expected-drift",
        "parent-gate-unbound",
        "parent-gate-drift",
    }
)
PARENT_SPECS: Final = (
    ("phase-0c", "phase-0c"),
    ("control-plane-baseline", "control-plane"),
)
JSON_OBJECT: Final = TypeAdapter(dict[str, InputValue])
JSON_TABLE: Final = TypeAdapter(dict[str, dict[str, InputValue]])
EXPECTED_CODES: Final[dict[str, str]] = {
    "incomplete_flow": "evidence-incomplete",
    "must_include_miss": "must-include-miss",
    "coordinate_defect": "coordinate-defect",
    "coverage_below_80": "coverage-below-80",
    "auto_created_operator_record": "operator-record-auto-created",
}


@dataclass(frozen=True, slots=True)
class _SyntheticParent:
    relative: str
    result: GateResult
    raw: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class _FaultContext:
    policy: GatePolicy
    policy_bytes: bytes
    policy_sha256: str
    parents: tuple[_SyntheticParent, ...]
    goldens: Mapping[str, Mapping[str, InputValue]]
    manifests: Mapping[str, Mapping[str, InputValue]]


def _load_json(path: Path) -> Mapping[str, InputValue]:
    return JSON_OBJECT.validate_json(path.read_bytes())


def _goldens() -> Mapping[str, Mapping[str, InputValue]]:
    fixtures = _load_json(Path("tests/goldens/reference/phase-1-technical/expected.json"))[
        "fixtures"
    ]
    return JSON_TABLE.validate_python(fixtures)


def _manifests() -> Mapping[str, Mapping[str, InputValue]]:
    manifest_dir = Path("tests/fixtures/manifests/phase-1-technical")
    return {path.stem: _load_json(path) for path in sorted(manifest_dir.glob("p1-ref-*.json"))}


def _seed_hash(domain: str) -> str:
    seed = f"phase1-fault-harness:v1:{domain}".encode()
    return hashlib.sha256(seed).hexdigest()


def _synthetic_parent(gate_id: str, relative: str) -> _SyntheticParent:
    result = GateResult(
        schema_version="gate-result-v1",
        gate_id=gate_id,
        gate_version=f"{SYNTHETIC_VERSION}:{gate_id}",
        policy_sha256=_seed_hash(f"{gate_id}:policy"),
        passed=True,
        evidence_bundles=(
            EvidenceBundleRef(
                bundle_sha256=_seed_hash(f"{gate_id}:bundle"),
                raw_evidence_sha256s=(_seed_hash(f"{gate_id}:bundle:raw"),),
            ),
        ),
        criteria_results=(
            CriterionResult(
                criterion_id=f"{gate_id}:synthetic-parent-pass",
                passed=True,
                raw_evidence_sha256s=(_seed_hash(f"{gate_id}:criterion:raw"),),
            ),
        ),
    )
    raw = canonical_gate_bytes(result)
    return _SyntheticParent(
        relative=relative,
        result=result,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _build_context(policy_path: Path) -> _FaultContext:
    source, _source_sha256 = gate_phase1.load_policy(policy_path)
    if source.gate_id != _PHASE1_GATE_ID:
        raise gate_phase1.GateP1Error(
            "policy-gate-mismatch",
            f"expected={_PHASE1_GATE_ID} actual={source.gate_id}",
        )
    parents = tuple(_synthetic_parent(gate_id, relative) for gate_id, relative in PARENT_SPECS)
    document = source.model_dump(mode="json")
    document["gate_version"] = SYNTHETIC_VERSION
    document["parent_gate_result_hashes"] = [parent.sha256 for parent in parents]
    document["toolchain_lock_sha256"] = sha256_file(gate_phase1.TOOLCHAIN_LOCK)
    policy = GatePolicy.model_validate(document)
    policy_bytes = canonical_gate_bytes(policy)
    return _FaultContext(
        policy=policy,
        policy_bytes=policy_bytes,
        policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        parents=parents,
        goldens=_goldens(),
        manifests=_manifests(),
    )


def _write_parents(root: Path, parents: tuple[_SyntheticParent, ...]) -> None:
    for parent in parents:
        target = root / parent.relative
        target.mkdir(parents=True, exist_ok=True)
        (target / "gate-result.json").write_bytes(parent.raw)


def _evaluate_fake(
    root: Path,
    knobs: FaultKnobs,
    context: _FaultContext,
) -> gate_phase1.Phase1GateOutcome:
    evidence = root / "evidence"
    _write_parents(root, context.parents)
    observations = synthesize(evidence, knobs, context.goldens, context.manifests)
    return gate_phase1.evaluate(
        context.policy,
        context.policy_sha256,
        evidence,
        work_id="fault-harness-work-id",
        drive=False,
        observations=observations,
    )


def run_fault_cli(fault_fixture: Path, policy_path: Path) -> int:
    try:
        spec = _load_json(fault_fixture)
        fault = str(spec.get("fault", ""))
        if fault not in FAULTS:
            print(f"{MARKER} fault-unknown fault={fault}", file=sys.stderr)
            return EXIT_FAULT
        context = _build_context(policy_path)
        print(f"{MARKER} mode=synthetic synthetic_policy_sha256={context.policy_sha256}")
        with tempfile.TemporaryDirectory(prefix="p1-fault-baseline-") as temporary:
            baseline = _evaluate_fake(Path(temporary), FaultKnobs(fault=""), context)
        with tempfile.TemporaryDirectory(prefix="p1-fault-probe-") as temporary:
            outcome = _evaluate_fake(Path(temporary), FaultKnobs(fault=fault), context)
    except (gate_phase1.GateP1Error, OSError, ValidationError, TypeError) as error:
        print(f"{MARKER} fault-error {error}", file=sys.stderr)
        return EXIT_FAULT
    expected = EXPECTED_CODES[fault]
    codes = [code for code, _detail in outcome.mismatches]
    infrastructure_codes = _INFRASTRUCTURE_MISMATCH_CODES.intersection(codes)
    detected = (
        baseline.result.passed
        and expected in codes
        and not outcome.result.passed
        and not infrastructure_codes
    )
    print(f"{MARKER} fault={fault} baseline={'PASS' if baseline.result.passed else 'FAIL'}")
    print(f"{MARKER} detected={str(detected).lower()} expected_code={expected}")
    print(f"{MARKER} codes={','.join(codes) or 'none'}")
    return EXIT_DETECTED if detected else EXIT_FAULT


def main(argv: list[str] | None = None) -> int:
    fault_fixture = Path(argv[0]) if argv else Path("fault.json")
    return run_fault_cli(fault_fixture, POLICY)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
