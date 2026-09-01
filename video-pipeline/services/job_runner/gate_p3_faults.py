"""Offline fault scenarios for the Phase-3 A/B Profile-swap Gate (``QA_FAULT_FIXTURE``).

Synthesizes the complete A/B evidence tree offline with one injected defect
each (no Resolve, no ffmpeg), writes ONE clearly synthetic canonical phase-2
parent result inside the temporary root only, derives a synthetic policy from
the supplied frozen policy (changing exactly the gate version, the parent
binding, and the toolchain lock), and runs the REAL evaluator with driving
disabled. The baseline no-fault synthesis must PASS every criterion (printed
as ``baseline=PASS``) — a fault is ``detected`` only when the baseline passed,
the probe failed with the expected typed code, and no infrastructure mismatch
fired. Exit 0 = detected, 2 = missed/unknown fault.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.foundation_io import sha256_file
from services.gates import (
    CriterionResult,
    EvidenceBundleRef,
    GatePolicy,
    GateResult,
    canonical_gate_bytes,
)
from services.gates.phase2 import PHASE_2_CRITERIA
from services.job_runner import gate_p3_checks, gate_phase3
from services.job_runner.gate_p3_fake_tree import (
    EXPECTED_CODES,
    FAULTS,
    FaultKnobs,
    synthesize_evidence,
)
from services.job_runner.gate_p3_models import SYNTHETIC_VERSION
from services.job_runner.gate_p3_regression import phase2_policy
from services.job_runner.gate_p3_scan import resolve_repo_path

MARKER: Final = "phase3-gate"
EXIT_DETECTED: Final = 0
EXIT_FAULT: Final = 2
POLICY: Final = Path("config/gates/phase-3-v3.json")
_PHASE3_GATE_ID: Final = "phase-3"
PARENT_RELATIVE: Final = "phase-2"
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
    parent: _SyntheticParent


def _seed_hash(domain: str) -> str:
    seed = f"phase3-fault-harness:v1:{domain}".encode()
    return hashlib.sha256(seed).hexdigest()


def _pinned_phase2_policy_sha256() -> str:
    return sha256_file(resolve_repo_path(phase2_policy()))


def _synthetic_parent() -> _SyntheticParent:
    """One canonical, clearly synthetic phase-2 parent binding the pinned policy."""
    result = GateResult(
        schema_version="gate-result-v1",
        gate_id=PARENT_RELATIVE,
        gate_version=f"{SYNTHETIC_VERSION}:{PARENT_RELATIVE}",
        policy_sha256=_pinned_phase2_policy_sha256(),
        passed=True,
        evidence_bundles=(
            EvidenceBundleRef(
                bundle_sha256=_seed_hash("phase-2:bundle"),
                raw_evidence_sha256s=(_seed_hash("phase-2:bundle:raw"),),
            ),
        ),
        criteria_results=tuple(
            CriterionResult(
                criterion_id=criterion,
                passed=True,
                raw_evidence_sha256s=(_seed_hash(f"phase-2:{criterion}:raw"),),
            )
            for criterion in PHASE_2_CRITERIA
        ),
    )
    raw = canonical_gate_bytes(result)
    return _SyntheticParent(
        relative=PARENT_RELATIVE,
        result=result,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _build_context(policy_path: Path) -> _FaultContext:
    source, _source_sha256 = gate_phase3.load_policy(policy_path)
    if source.gate_id != _PHASE3_GATE_ID:
        raise gate_phase3.GateP3Error(
            "policy-gate-mismatch",
            f"expected={_PHASE3_GATE_ID} actual={source.gate_id}",
        )
    parent = _synthetic_parent()
    document = source.model_dump(mode="json")
    document["gate_version"] = SYNTHETIC_VERSION
    document["parent_gate_result_hashes"] = [parent.sha256]
    document["toolchain_lock_sha256"] = sha256_file(
        resolve_repo_path(gate_p3_checks.LOCK_PATH)
    )
    policy = GatePolicy.model_validate(document)
    policy_bytes = canonical_gate_bytes(policy)
    return _FaultContext(
        policy=policy,
        policy_bytes=policy_bytes,
        policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
        parent=parent,
    )


def _write_parent(root: Path, parent: _SyntheticParent) -> None:
    target = root / parent.relative
    target.mkdir(parents=True, exist_ok=True)
    (target / "gate-result.json").write_bytes(parent.raw)


def _evaluate_fake(
    root: Path,
    knobs: FaultKnobs,
    context: _FaultContext,
) -> gate_phase3.Phase3GateOutcome:
    _write_parent(root, context.parent)
    tree = synthesize_evidence(root, knobs)
    return gate_phase3.evaluate(
        context.policy,
        context.policy_sha256,
        tree.evidence,
        drive=False,
        observation=tree.observation,
    )


def run_fault_cli(fault_fixture: Path, policy_path: Path) -> int:
    try:
        spec = json.loads(fault_fixture.read_bytes())
    except (OSError, ValueError) as error:
        print(f"{MARKER} fault-unreadable {error}", file=sys.stderr)
        return EXIT_FAULT
    if not isinstance(spec, dict):
        print(f"{MARKER} fault-unreadable {fault_fixture} is not a JSON object", file=sys.stderr)
        return EXIT_FAULT
    fault = str(spec.get("fault", ""))
    if fault not in FAULTS:
        print(f"{MARKER} fault-unknown fault={fault}", file=sys.stderr)
        return EXIT_FAULT
    try:
        context = _build_context(policy_path)
        print(f"{MARKER} mode=synthetic synthetic_policy_sha256={context.policy_sha256}")
        with tempfile.TemporaryDirectory(prefix="p3-fault-baseline-") as temporary:
            baseline = _evaluate_fake(Path(temporary), FaultKnobs(fault=""), context)
        with tempfile.TemporaryDirectory(prefix="p3-fault-probe-") as temporary:
            probe = _evaluate_fake(Path(temporary), FaultKnobs(fault=fault), context)
    except (gate_phase3.GateP3Error, OSError, ValidationError, TypeError, ValueError) as error:
        print(f"{MARKER} fault-error {error}", file=sys.stderr)
        return EXIT_FAULT
    expected = EXPECTED_CODES[fault]
    codes = [code for code, _detail in probe.mismatches]
    infrastructure_codes = _INFRASTRUCTURE_MISMATCH_CODES.intersection(codes)
    detected = (
        baseline.result.passed
        and expected in codes
        and not probe.result.passed
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
