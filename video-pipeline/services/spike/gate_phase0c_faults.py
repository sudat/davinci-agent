"""Offline fault scenarios for the Phase-0C gate (``QA_FAULT_FIXTURE`` mode).

Each fault synthesizes a complete fake evidence tree (no ffmpeg, no Resolve)
with one injected defect, runs the REAL 0C evaluator over it, and prints the
observation. Exit code is ``EXIT_FAULT`` whether or not the fault was
detected; the printed observation discriminates. The stale-event fault adds
the shared stop-marker demonstration (a rerun is refused while the marker is
present), and the resolve-requirement fault proves the 0C gate path refuses
any dependency on the Resolve bridge (static import scan plus a runtime
``sys.modules`` leak check).
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

from services.contracts.primitives import StrictModel
from services.foundation_io import sha256_file
from services.gates import GatePolicy
from services.spike.gate_models import EXIT_FAULT
from services.spike.gate_phase0c_evaluate import (
    Evaluate0cInputs,
    Evaluate0cOutcome,
    evaluate0c,
)
from services.spike.gate_phase0c_fakes import FaultKnobs0c, synthesize0c
from services.spike.gate_phase0c_models import (
    FAULT_KINDS,
    GATE0C_MARKER,
)
from services.spike.stop_rules import GateStopRecord, record_stop, stop_recorded

GATE0C_PACKAGE: Final = Path("services/spike")
EVIDENCE_ROOT_HINT: Final = "phase-0c"


class Gate0cFaultSpec(StrictModel):
    fault: str


def _knobs(fault: str) -> FaultKnobs0c:
    return FaultKnobs0c(
        wrong_decision=fault == "wrong_decision",
        auto_applied_ambiguous=fault == "auto_applied_ambiguous",
        changed_unrelated_item=fault == "changed_unrelated_item",
        stale_event=fault == "stale_event",
    )


def _gate0c_modules() -> tuple[Path, ...]:
    return tuple(sorted(GATE0C_PACKAGE.glob("gate_phase0c*.py")))


def _resolve_guard() -> bool:
    """The 0C gate path must never depend on the Resolve bridge."""

    offenders: list[str] = []
    for path in _gate0c_modules():
        tree = ast.parse(path.read_bytes())
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules = [node.module]
            offenders.extend(
                f"{path.name}:{module}" for module in modules if "resolve_bridge" in module
            )
    if offenders:
        print(f"{GATE0C_MARKER} resolve-guard offenders={offenders}", file=sys.stderr)
        return False
    names = [f"services.spike.{path.stem}" for path in _gate0c_modules()]
    code = (
        "import sys\n"
        + "".join(f"import {name}\n" for name in names)
        + "leaked = [m for m in sys.modules if m.startswith('services.resolve_bridge')]\n"
        "print('resolve-modules=' + (','.join(leaked) or 'none'))\n"
        "raise SystemExit(1 if leaked else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path.cwd(),
    )
    if result.returncode != 0 or "resolve-modules=none" not in result.stdout:
        print(
            f"{GATE0C_MARKER} resolve-guard runtime leak: {result.stdout}{result.stderr}",
            file=sys.stderr,
        )
        return False
    return True


def _parent_result() -> Path:
    override = os.environ.get("PHASE0B_RESULT")
    if override:
        return Path(override)
    raise FileNotFoundError(
        "frozen phase-0b gate result not found: set PHASE0B_RESULT for 0C fault mode"
    )


def _report(outcome: Evaluate0cOutcome, expected_code: str, evidence: Path) -> int:
    for row in outcome.mismatches:
        print(f"{GATE0C_MARKER} mismatch code={row.code} detail={row.detail}", file=sys.stderr)
    if outcome.stop_triggered:
        print(
            f"{GATE0C_MARKER} STOP code={outcome.stop_criterion} reason={outcome.stop_reason}"
        )
        marker = record_stop(
            evidence, outcome.stop_criterion, outcome.stop_reason, gate_id="phase-0c"
        )
        refused = stop_recorded(evidence)
        print(f"{GATE0C_MARKER} stop-marker-written path={marker.name}")
        print(
            f"{GATE0C_MARKER} rerun-refused={str(refused is not None).lower()} "
            f"criterion={refused.criterion_id if refused else ''}"
        )
        if outcome.stop_criterion != expected_code:
            print(
                f"ERROR: expected stop {expected_code} but got {outcome.stop_criterion}",
                file=sys.stderr,
            )
        return EXIT_FAULT
    observed = expected_code in {row.code for row in outcome.mismatches}
    print(f"{GATE0C_MARKER} fault-observed={str(observed).lower()} code={expected_code}")
    if not observed:
        print("ERROR: fault not observed", file=sys.stderr)
    return EXIT_FAULT


def run_fault_cli(spec_path: Path, policy_path: Path) -> int:
    try:
        spec = Gate0cFaultSpec.model_validate_json(spec_path.read_bytes())
        policy = GatePolicy.model_validate_json(policy_path.read_bytes())
    except (OSError, ValueError) as error:
        print(f"{GATE0C_MARKER} fault-fixture invalid: {error}", file=sys.stderr)
        return EXIT_FAULT
    if spec.fault not in FAULT_KINDS:
        print(f"{GATE0C_MARKER} fault-fixture invalid: unknown fault {spec.fault!r}")
        return EXIT_FAULT
    if spec.fault == "resolve_requirement":
        clean = _resolve_guard()
        print(
            f"{GATE0C_MARKER} fault-observed={str(clean).lower()} "
            f"code={FAULT_KINDS['resolve_requirement']}"
        )
        return EXIT_FAULT
    try:
        parent = _parent_result()
    except FileNotFoundError as error:
        print(f"{GATE0C_MARKER} fault-fixture invalid: {error}", file=sys.stderr)
        return EXIT_FAULT
    with tempfile.TemporaryDirectory(prefix="gate0c-fault-") as scratch:
        evidence = Path(scratch) / EVIDENCE_ROOT_HINT
        synthesize0c(evidence, _knobs(spec.fault))
        outcome = evaluate0c(
            Evaluate0cInputs(
                policy=policy,
                policy_sha256=sha256_file(policy_path),
                evidence=evidence,
                parent_result_path=parent,
            )
        )
        return _report(outcome, FAULT_KINDS[spec.fault], evidence)


__all__ = [
    "GateStopRecord",
    "run_fault_cli",
]
