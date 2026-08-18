"""Offline fault scenarios for the Phase-1 Technical Gate (``QA_FAULT_FIXTURE``).

Synthesizes the full five-fixture evidence tree from the frozen goldens with
one injected defect each (no chain run, no ffmpeg, no Resolve), then runs the
REAL evaluator with driving disabled. The baseline no-fault synthesis must
PASS every criterion (printed as ``baseline=PASS``) — a fault is ``detected``
only when the gate FAILS and the recomputed mismatches carry the expected
code. Exit 0 = detected, 2 = missed/unknown fault.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.job_runner.gate_p1_fake_tree import synthesize
from services.job_runner.gate_p1_fakes import FAULTS, FaultKnobs
from services.job_runner.gate_phase1 import Phase1GateOutcome, evaluate, load_policy

MARKER: Final = "phase1-gate"
EXIT_DETECTED: Final = 0
EXIT_FAULT: Final = 2
POLICY: Final = Path("config/gates/phase-1-technical-v1.json")
ATTEMPT: Final = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
EXPECTED_CODES: Final[dict[str, str]] = {
    "incomplete_flow": "evidence-incomplete",
    "must_include_miss": "must-include-miss",
    "coordinate_defect": "coordinate-defect",
    "coverage_below_80": "coverage-below-80",
    "auto_created_operator_record": "operator-record-auto-created",
}


def _load_json(path: Path) -> dict[str, object]:
    document = json.loads(path.read_bytes())
    if not isinstance(document, dict):
        raise TypeError(f"not a JSON object: {path}")
    return document


def _goldens() -> dict[str, dict[str, object]]:
    fixtures = _load_json(Path("tests/goldens/reference/phase-1-technical/expected.json"))[
        "fixtures"
    ]
    if not isinstance(fixtures, dict):
        raise TypeError("golden fixtures table is not an object")
    return {str(key): value for key, value in fixtures.items() if isinstance(value, dict)}


def _manifests() -> dict[str, dict[str, object]]:
    manifest_dir = Path("tests/fixtures/manifests/phase-1-technical")
    return {path.stem: _load_json(path) for path in sorted(manifest_dir.glob("p1-ref-*.json"))}


def _link_parents(root: Path) -> None:
    for relative in ("phase-0c", "control-plane"):
        target = root / relative
        target.mkdir(parents=True, exist_ok=True)
        (target / "gate-result.json").write_bytes(
            (ATTEMPT / relative / "gate-result.json").read_bytes()
        )


def _evaluate_fake(
    label: str,
    knobs: FaultKnobs,
    goldens: dict[str, dict[str, object]],
    manifests: dict[str, dict[str, object]],
) -> Phase1GateOutcome:
    policy, policy_sha256 = load_policy(POLICY)
    with tempfile.TemporaryDirectory(prefix=f"p1-fault-{label}-") as tmp:
        root = Path(tmp)
        evidence = root / "evidence"
        _link_parents(root)
        observations = synthesize(evidence, knobs, goldens, manifests)  # type: ignore[arg-type]
        return evaluate(
            policy,
            policy_sha256,
            evidence,
            work_id="fault-harness-work-id",
            drive=False,
            observations=observations,
        )


def run_fault_cli(fault_fixture: Path, policy_path: Path) -> int:
    del policy_path  # the frozen in-repo policy is the only fault target
    spec = _load_json(fault_fixture)
    fault = str(spec.get("fault", ""))
    if fault not in FAULTS:
        print(f"{MARKER} fault-unknown fault={fault}", file=sys.stderr)
        return EXIT_FAULT
    goldens = _goldens()
    manifests = _manifests()
    try:
        baseline = _evaluate_fake("baseline", FaultKnobs(fault=""), goldens, manifests)
        outcome = _evaluate_fake("fault", FaultKnobs(fault=fault), goldens, manifests)
    except (OSError, ValidationError, TypeError) as error:
        print(f"{MARKER} fault-error {error}", file=sys.stderr)
        return EXIT_FAULT
    expected = EXPECTED_CODES[fault]
    codes = [code for code, _detail in outcome.mismatches]
    detected = expected in codes and not outcome.result.passed
    print(f"{MARKER} fault={fault} baseline={'PASS' if baseline.result.passed else 'FAIL'}")
    print(f"{MARKER} detected={str(detected).lower()} expected_code={expected}")
    print(f"{MARKER} codes={','.join(codes) or 'none'}")
    return EXIT_DETECTED if detected else EXIT_FAULT


def main(argv: list[str] | None = None) -> int:
    fault_fixture = Path(argv[0]) if argv else Path("fault.json")
    return run_fault_cli(fault_fixture, POLICY)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
