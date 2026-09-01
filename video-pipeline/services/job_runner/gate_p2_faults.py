"""Offline fault scenarios for the Phase-2 gate (``QA_FAULT_FIXTURE``).

Synthesizes the full five-fixture evidence tree offline with one injected
defect each (no Resolve, no ffmpeg), then runs the REAL evaluator with
driving disabled against a truthful temporary synthetic context: three
canonical synthetic parents, a typed clearly-synthetic H1
checkpoint/display-receipt pair, and a typed freeze receipt bound to the
derived policy (see ``gate_p2_synthetic_chain``). The baseline no-fault
synthesis must PASS every criterion (printed as ``baseline=PASS``) — a
fault is ``detected`` only when the baseline passed, the probe failed with
the expected typed code, and no infrastructure mismatch fired. Exit
0 = detected, 2 = missed/unknown fault.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.job_runner.gate_p2_fake_tree import (
    EXPECTED_CODES,
    FAULTS,
    FaultKnobs,
    synthesize_evidence,
)
from services.job_runner.gate_p2_synthetic_chain import (
    SYNTHETIC_VERSION,
    build_synthetic_context,
)
from services.job_runner.gate_p2_synthetic_chain import (
    FaultContext as _FaultContext,
)
from services.job_runner.gate_phase2 import GateP2Error, evaluate, load_policy

if TYPE_CHECKING:
    from services.gates import GateResult
    from services.job_runner.gate_p2_models import Phase2ExitMarker

MARKER: Final = "phase2-gate"
EXIT_DETECTED: Final = 0
EXIT_FAULT: Final = 2
POLICY: Final = Path("config/gates/phase-2-v4.json")
CURRENT_CHAIN_VERSION: Final = "v4"
_PHASE2_GATE_ID: Final = "phase-2"
_INFRASTRUCTURE_MISMATCH_CODES: Final = frozenset(
    {
        "toolchain-lock-drift",
        "fixture-manifest-drift",
        "golden-index-drift",
        "golden-expected-drift",
        "parent-gate-unbound",
        "parent-gate-drift",
        "freeze-receipt-missing",
        "prerequisite-stale",
        "prerequisite-binding-drift",
    }
)


@dataclass(frozen=True, slots=True)
class FakeOutcome:
    result: GateResult
    marker: Phase2ExitMarker | None
    mismatches: tuple[tuple[str, str], ...]
    expected_code: str


def _build_context(policy_path: Path) -> _FaultContext:
    """Derive the synthetic context from the SUPPLIED current chain policy."""

    source, _source_sha = load_policy(policy_path)
    if source.gate_id != _PHASE2_GATE_ID:
        raise GateP2Error(
            "policy-gate-mismatch",
            f"expected gate {_PHASE2_GATE_ID}, actual {source.gate_id}",
        )
    if source.gate_version != CURRENT_CHAIN_VERSION:
        raise GateP2Error(
            "policy-gate-mismatch",
            f"expected the current chain policy version {CURRENT_CHAIN_VERSION}, "
            f"actual {source.gate_version} (stale policies cannot seed a "
            "synthetic run)",
        )
    return build_synthetic_context(source, policy_path)


def _write_parents(root: Path, context: _FaultContext) -> None:
    for parent in context.parents:
        target = root / parent.relative
        target.mkdir(parents=True, exist_ok=True)
        (target / "gate-result.json").write_bytes(parent.raw)


def _write_checkpoint(root: Path, context: _FaultContext) -> Path:
    checkpoint_path = root / context.checkpoint_relative
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    (checkpoint_path.parent / "display-receipt.json").write_bytes(
        context.display_receipt_raw
    )
    checkpoint_path.write_bytes(context.checkpoint_raw)
    return checkpoint_path


def _evaluate_fake(root: Path, knobs: FaultKnobs, context: _FaultContext) -> FakeOutcome:
    _write_parents(root, context)
    checkpoint_path = _write_checkpoint(root, context)
    receipt = context.freeze_receipt.model_copy(
        update={"prerequisite_checkpoint_path": str(checkpoint_path)}
    )
    tree = synthesize_evidence(root, knobs)
    outcome = evaluate(
        context.policy,
        context.policy_sha256,
        tree.evidence,
        drive=False,
        observations=tree.observations,
        freeze_receipt=receipt,
    )
    expected = EXPECTED_CODES.get(knobs.fault, "")
    return FakeOutcome(
        result=outcome.result,
        marker=outcome.exit_marker,
        mismatches=outcome.mismatches,
        expected_code=expected,
    )


def run_fault_cli(fault_fixture: Path, policy_path: Path) -> int:
    try:
        spec = json.loads(fault_fixture.read_bytes())
    except (OSError, ValueError) as error:
        print(f"{MARKER} fault-unreadable {error}", file=sys.stderr)
        return EXIT_FAULT
    if not isinstance(spec, dict):
        print(f"{MARKER} fault-unreadable spec is not an object", file=sys.stderr)
        return EXIT_FAULT
    fault = str(spec.get("fault", ""))
    if fault not in FAULTS:
        print(f"{MARKER} fault-unknown fault={fault}", file=sys.stderr)
        return EXIT_FAULT
    try:
        context = _build_context(policy_path)
        print(f"{MARKER} mode=synthetic synthetic_policy_sha256={context.policy_sha256}")
        with tempfile.TemporaryDirectory(prefix="p2-fault-baseline-") as tmp:
            baseline = _evaluate_fake(Path(tmp), FaultKnobs(), context)
        with tempfile.TemporaryDirectory(prefix="p2-fault-probe-") as tmp:
            probe = _evaluate_fake(Path(tmp), FaultKnobs(fault=fault), context)
    except (GateP2Error, OSError, ValidationError, TypeError, ValueError) as error:
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
    print(
        f"{MARKER} fault={fault} "
        f"baseline={'PASS' if baseline.result.passed else 'FAIL'}"
    )
    print(f"{MARKER} detected={str(detected).lower()} expected_code={probe.expected_code}")
    print(f"{MARKER} codes={','.join(codes) or 'none'}")
    return EXIT_DETECTED if detected else EXIT_FAULT


def main(argv: list[str] | None = None) -> int:
    fault_fixture = Path(argv[0]) if argv else Path("fault.json")
    return run_fault_cli(fault_fixture, POLICY)


__all__ = [
    "EXIT_DETECTED",
    "EXIT_FAULT",
    "MARKER",
    "POLICY",
    "SYNTHETIC_VERSION",
    "_FaultContext",
    "_build_context",
    "_evaluate_fake",
    "_write_checkpoint",
    "_write_parents",
    "main",
    "run_fault_cli",
]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
