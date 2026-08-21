"""Offline fault scenarios for the Phase-2 gate (``QA_FAULT_FIXTURE``).

Synthesizes the full five-fixture evidence tree offline with one injected
defect each (no Resolve, no ffmpeg), then runs the REAL evaluator with
driving disabled. The baseline no-fault synthesis must PASS every
criterion (printed as ``baseline=PASS``) — a fault is ``detected`` only
when the gate FAILS and the recomputed mismatches carry the expected
code. Exit 0 = detected, 2 = missed/unknown fault.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.job_runner.gate_p2_fake_tree import (
    EXPECTED_CODES,
    FAULTS,
    FaultKnobs,
    synthesize_evidence,
)
from services.job_runner.gate_phase2 import evaluate, load_policy

if TYPE_CHECKING:
    from services.gates import GatePolicy, GateResult
    from services.job_runner.gate_p2_models import Phase2ExitMarker

MARKER: Final = "phase2-gate"
EXIT_DETECTED: Final = 0
EXIT_FAULT: Final = 2
POLICY: Final = Path("config/gates/phase-2-v1.json")
ATTEMPT: Final = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
PARENTS: Final = ("phase-0a", "phase-0b", "phase-1-technical")
def _cascade_receipt() -> Path:
    """The receipt matching the current frozen phase-2 gate version."""
    import json  # noqa: PLC0415

    result = json.loads((ATTEMPT / "phase-2" / "gate-result.json").read_text())
    version = result.get("gate_version")
    if isinstance(version, str) and version != "v1":
        cascaded = ATTEMPT / "gate-cascade" / "receipts" / f"phase-2-{version}.json"
        if cascaded.is_file():
            return cascaded
    return ATTEMPT / "task-47-freeze-receipt.json"


RECEIPT: Final = _cascade_receipt()


@dataclass(frozen=True, slots=True)
class FakeOutcome:
    result: GateResult
    marker: Phase2ExitMarker | None
    mismatches: tuple[tuple[str, str], ...]
    expected_code: str


def _link_parents(root: Path) -> None:
    for relative in PARENTS:
        target = root / relative
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            ATTEMPT / relative / "gate-result.json", target / "gate-result.json"
        )


def evaluate_fake(
    policy: GatePolicy, policy_sha256: str, root: Path, knobs: FaultKnobs
) -> FakeOutcome:
    """Synthesize one evidence tree and run the REAL evaluator over it."""

    from services.job_runner.gate_phase2 import load_receipt  # noqa: PLC0415

    _link_parents(root)
    tree = synthesize_evidence(root, knobs)
    outcome = evaluate(
        policy,
        policy_sha256,
        tree.evidence,
        drive=False,
        observations=tree.observations,
        freeze_receipt=load_receipt(RECEIPT, policy_sha256),
    )
    expected = EXPECTED_CODES.get(knobs.fault, "")
    return FakeOutcome(
        result=outcome.result,
        marker=outcome.exit_marker,
        mismatches=outcome.mismatches,
        expected_code=expected,
    )


def run_fault_cli(fault_fixture: Path, policy_path: Path) -> int:
    del policy_path  # the frozen in-repo policy is the only fault target
    import json  # noqa: PLC0415

    spec = json.loads(fault_fixture.read_bytes())
    fault = str(spec.get("fault", ""))
    if fault not in FAULTS:
        print(f"{MARKER} fault-unknown fault={fault}", file=sys.stderr)
        return EXIT_FAULT
    try:
        policy, policy_sha256 = load_policy(POLICY)
        with tempfile.TemporaryDirectory(prefix="p2-fault-baseline-") as tmp:
            baseline = evaluate_fake(policy, policy_sha256, Path(tmp), FaultKnobs())
        with tempfile.TemporaryDirectory(prefix="p2-fault-probe-") as tmp:
            probe = evaluate_fake(policy, policy_sha256, Path(tmp), FaultKnobs(fault=fault))
    except (OSError, ValueError) as error:
        print(f"{MARKER} fault-error {error}", file=sys.stderr)
        return EXIT_FAULT
    codes = [code for code, _detail in probe.mismatches]
    detected = probe.expected_code in codes and not probe.result.passed
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
