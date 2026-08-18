"""The Phase-1 Technical Gate entry (Todo 46): evaluate + the H1 stop.

Loads the frozen policy (canonical), verifies the optional freeze receipt,
refuses while a cross-gate stop marker is present, drives the REAL five
fixture chain twice each plus the declared review-correction sequence,
recomputes every frozen criterion from raw evidence, and writes the canonical
``gate-result.json``. On a technical PASS the gate RECORDS THE H1 STOP: it
writes ``h1-waiting.json`` (next_checkpoint=H1, status=NEEDS_HUMAN, bound to
the execution work id and every fixture's bundle/preview hashes) next to the
evidence and exits 0 — the plan checkbox stays unchecked until the owner
completes the real-episode EDITORIAL_APPROVED sequence. Human time remains
``not_evaluated``; the gate never counts a fixture approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
from services.foundation_io import atomic_write
from services.gates import GatePolicy, GateResult
from services.gates.phase1_technical import PHASE_1_TECHNICAL_CRITERIA, PHASE_1_TECHNICAL_FIXTURES
from services.gates.serialization import canonical_gate_bytes
from services.job_runner.gate_p1_checks import (
    CheckState,
    check_approvals,
    check_bindings,
    check_e2e,
    check_must_include,
)
from services.job_runner.gate_p1_drive import drive_all_fixtures, drive_approvals
from services.job_runner.gate_p1_flow_checks import (
    check_coverage,
    check_determinism,
    check_placement,
)
from services.job_runner.gate_p1_models import (
    APPROVALS_OBSERVATION,
    RESULT_NAME,
    FixtureObservation,
)
from services.job_runner.gate_p1_result import build_result, h1_marker
from services.spike.stop_rules import stop_recorded

MARKER: Final = "phase1-gate"
EXIT_PASS: Final = 0
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 2
EXIT_STOP: Final = 3
GOLDENS_DIR: Final = Path("tests/goldens/reference/phase-1-technical")
MANIFEST_DIR: Final = Path("tests/fixtures/manifests/phase-1-technical")
TOOLCHAIN_LOCK: Final = Path("config/toolchains/phase-1-technical-v1.json")
LEDGER: Final = Path("../.omo/start-work/ledger.jsonl")
PLAN_PATH: Final = Path("../.omo/plans/foundation-video-pipeline.md")


class GateP1Error(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Phase1GateOutcome:
    result: GateResult
    result_path: Path
    h1_waiting: Path | None
    mismatches: tuple[tuple[str, str], ...]


def load_policy(path: Path) -> tuple[GatePolicy, str]:
    raw = path.read_bytes()
    policy = GatePolicy.model_validate_json(raw)
    if raw != canonical_gate_bytes(policy):
        raise GateP1Error("policy-noncanonical", str(path))
    return policy, hashlib.sha256(raw).hexdigest()


def resolve_parents(evidence: Path) -> dict[str, Path]:
    root = evidence.parent
    found: dict[str, Path] = {}
    for gate_id, relative in (
        ("phase-0c", "phase-0c"),
        ("control-plane-baseline", "control-plane"),
    ):
        candidate = root / relative / RESULT_NAME
        if candidate.is_file():
            found[gate_id] = candidate
    return found


def load_goldens() -> dict[str, dict[str, object]]:
    document = json.loads((GOLDENS_DIR / "expected.json").read_bytes())
    if not isinstance(document, dict) or not isinstance(document.get("fixtures"), dict):
        raise GateP1Error("golden-malformed", str(GOLDENS_DIR / "expected.json"))
    fixtures = document["fixtures"]
    return {
        str(key): value
        for key, value in fixtures.items()
        if isinstance(value, dict)
    }


def restore_work_id(override: str | None) -> str:
    if override is not None:
        return override
    from services.execution.work_init import (  # noqa: PLC0415
        WorkInitializationError,
        restore_for_plan,
    )

    plan_path = PLAN_PATH.resolve()
    plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    try:
        record = restore_for_plan(LEDGER.resolve(), plan_path, plan_hash)
    except (WorkInitializationError, OSError) as error:
        raise GateP1Error("work-id-unrestorable", str(error)) from error
    return record.execution_work_id


def evaluate(  # noqa: PLR0913 (gate contract: policy + evidence + stop seams)
    policy: GatePolicy,
    policy_sha256: str,
    evidence: Path,
    *,
    work_id: str,
    drive: bool = True,
    observations: dict[str, FixtureObservation] | None = None,
) -> Phase1GateOutcome:
    state = CheckState()
    parents = resolve_parents(evidence)
    check_bindings(policy, MANIFEST_DIR, GOLDENS_DIR, TOOLCHAIN_LOCK, parents, state)
    if drive:
        observations = drive_all_fixtures(evidence, MANIFEST_DIR)
        drive_approvals(evidence)
    if observations is None:
        raise GateP1Error("evidence-incomplete", "no observations supplied")
    check_approvals(evidence / APPROVALS_OBSERVATION / "observation.json", state)
    goldens = load_goldens()
    manifests = {
        fixture_id: Phase1TechnicalFixtureManifest.model_validate_json(
            (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
        )
        for fixture_id in PHASE_1_TECHNICAL_FIXTURES
    }
    for fixture_id in PHASE_1_TECHNICAL_FIXTURES:
        observation = observations[fixture_id]
        golden = goldens[fixture_id]
        manifest = manifests[fixture_id]
        check_e2e(observation, golden, manifest, state)
        check_must_include(observation, golden, manifest, state)
        check_placement(observation, golden, state)
        check_determinism(observation, state)
    check_coverage(observations, goldens, state)
    for criterion in PHASE_1_TECHNICAL_CRITERIA:
        if not state.evidence[criterion]:
            state.fail(criterion, "evidence-incomplete", criterion)
    h1_path: Path | None = None
    if all(state.criteria.values()):
        h1_path = h1_marker(evidence, policy_sha256, work_id, observations)
    result = build_result(policy, policy_sha256, evidence, state)
    result_path = evidence / RESULT_NAME
    atomic_write(result_path, canonical_gate_bytes(result))
    return Phase1GateOutcome(
        result=result,
        result_path=result_path,
        h1_waiting=h1_path,
        mismatches=tuple((row.code, row.detail) for row in state.mismatches),
    )


def run_phase1_gate(arguments: argparse.Namespace) -> int:
    try:
        policy, policy_sha256 = load_policy(arguments.policy)
        work_id = restore_work_id(arguments.work_id)
    except (OSError, ValidationError, GateP1Error) as error:
        print(f"{MARKER} FAIL code=gate-inputs {error}", file=sys.stderr)
        return EXIT_USAGE
    stopped = stop_recorded(arguments.evidence)
    if stopped is not None:
        print(
            f"{MARKER} STOP-REFUSED gate={stopped.gate_id} "
            f"criterion={stopped.criterion_id} reason={stopped.reason}"
        )
        return EXIT_STOP
    arguments.evidence.mkdir(parents=True, exist_ok=True)
    try:
        outcome = evaluate(policy, policy_sha256, arguments.evidence, work_id=work_id)
    except (OSError, ValidationError, GateP1Error) as error:
        print(f"{MARKER} FAIL code=drive-error {error}", file=sys.stderr)
        return EXIT_FAIL
    for row in outcome.result.criteria_results:
        print(f"{MARKER} criterion {row.criterion_id} {'PASS' if row.passed else 'FAIL'}")
    for code, detail in outcome.mismatches:
        print(f"{MARKER} mismatch code={code} detail={detail}")
    verdict = "PASS" if outcome.result.passed else "FAIL"
    print(f"{MARKER} {verdict} gate-result={outcome.result_path}")
    if not outcome.result.passed or outcome.h1_waiting is None:
        return EXIT_FAIL
    print(f"{MARKER} next_checkpoint=H1 status=NEEDS_HUMAN")
    print(f"{MARKER} h1-waiting={outcome.h1_waiting}")
    return EXIT_PASS


def parser() -> argparse.ArgumentParser:
    entry = argparse.ArgumentParser(prog="run_gate phase-1-technical")
    entry.add_argument("--policy", type=Path, required=True)
    entry.add_argument("--evidence", type=Path, required=True)
    entry.add_argument("--work-id", default=None)
    return entry


__all__ = [
    "EXIT_FAIL",
    "EXIT_PASS",
    "EXIT_STOP",
    "EXIT_USAGE",
    "GOLDENS_DIR",
    "MANIFEST_DIR",
    "MARKER",
    "TOOLCHAIN_LOCK",
    "Phase1GateOutcome",
    "evaluate",
    "load_policy",
    "parser",
    "restore_work_id",
    "run_phase1_gate",
]
