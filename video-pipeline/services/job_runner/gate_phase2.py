"""The Phase-2 Finalization Gate entry (Todo 54): evaluate + the exit marker.

Loads the frozen policy (canonical), verifies the freeze receipt binding
and revalidates the H1 prerequisite checkpoint, refuses while a cross-gate
stop marker is present, drives the REAL five-fixture chain (offline typed
fault probes plus the live Resolve builds when a bridge is available),
recomputes every frozen criterion from raw evidence, and writes the
canonical ``gate-result.json`` plus the truthful ``phase-2-exit`` marker
(fixture Final records stay ``fixture_only``; never publication
decisions). Exit codes: 0 pass, 1 fail, 2 usage/policy error, 3 stop.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.fixtures.models import Phase2FreezeReceipt
from services.foundation_io import atomic_write, sha256_file
from services.gates import GatePolicy, GateResult
from services.gates.serialization import canonical_gate_bytes
from services.job_runner.gate_p2_checks import check_all
from services.job_runner.gate_p2_drive import drive_all_fixtures
from services.job_runner.gate_p2_models import (
    EXIT_MARKER_NAME,
    RESULT_NAME,
    P2FixtureObservation,
    Phase2ExitMarker,
)
from services.job_runner.gate_p2_result import build_result, exit_marker
from services.resolve_bridge.connection import BridgeConnectionError, ResolveConnection
from services.resolve_bridge.launch import launch_and_connect
from services.resolve_bridge.readiness import load_host_report
from services.spike.stop_rules import stop_recorded
from services.toolchain.models import load_lock

MARKER: Final = "phase2-gate"
EXIT_PASS: Final = 0
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 2
EXIT_STOP: Final = 3
DEFAULT_RECEIPT: Final = "task-47-freeze-receipt.json"
DEFAULT_HOST_REPORT: Final = "resolve-host.json"


class GateP2Error(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Phase2GateOutcome:
    result: GateResult
    result_path: Path
    exit_marker: Phase2ExitMarker | None
    mismatches: tuple[tuple[str, str], ...]


def load_policy(path: Path) -> tuple[GatePolicy, str]:
    raw = path.read_bytes()
    policy = GatePolicy.model_validate_json(raw)
    if raw != canonical_gate_bytes(policy):
        raise GateP2Error("policy-noncanonical", str(path))
    return policy, hashlib.sha256(raw).hexdigest()


def load_receipt(path: Path, policy_sha256: str) -> Phase2FreezeReceipt:
    try:
        receipt = Phase2FreezeReceipt.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise GateP2Error("freeze-receipt-invalid", str(error)) from error
    if receipt.policy_sha256 != policy_sha256:
        raise GateP2Error("freeze-receipt-binding-drift", "receipt binds a different policy")
    return receipt


ATTEMPT_ROOT: Final = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)


def default_receipt(evidence: Path) -> Path:
    """The cascade receipt matching the frozen phase-2 gate version, if any."""
    import json  # noqa: PLC0415

    result_path = ATTEMPT_ROOT / "phase-2" / "gate-result.json"
    if result_path.is_file():
        version = json.loads(result_path.read_bytes()).get("gate_version")
        if isinstance(version, str) and version != "v1":
            cascaded = ATTEMPT_ROOT / "gate-cascade" / "receipts" / f"phase-2-{version}.json"
            if cascaded.is_file():
                return cascaded
    return evidence.parent / DEFAULT_RECEIPT


def _media_bins() -> tuple[Path, Path]:
    lock = load_lock(Path("config/toolchains/phase-2-v1.json"))
    return Path(lock.ffmpeg.ffmpeg.path), Path(lock.ffmpeg.ffprobe.path)


def resolve_connection(evidence: Path) -> ResolveConnection:
    report_path = os.environ.get("RESOLVE_HOST_REPORT")
    candidates = [Path(report_path)] if report_path else []
    candidates.append(evidence.parent / DEFAULT_HOST_REPORT)
    for candidate in candidates:
        if candidate.is_file():
            return launch_and_connect(load_host_report(candidate))
    raise GateP2Error(
        "resolve-unavailable",
        "no resolve host report (set RESOLVE_HOST_REPORT or place "
        f"{DEFAULT_HOST_REPORT} beside the evidence dir)",
    )


def evaluate(  # noqa: PLR0913 (gate contract: policy + receipt + evidence + drive seams)
    policy: GatePolicy,
    policy_sha256: str,
    evidence: Path,
    *,
    freeze_receipt: Phase2FreezeReceipt | None = None,
    connection: ResolveConnection | None = None,
    drive: bool = True,
    observations: dict[str, P2FixtureObservation] | None = None,
) -> Phase2GateOutcome:
    from services.gates.prerequisite import (  # noqa: PLC0415
        PrerequisiteError,
        verify_editorial_approved_checkpoint,
    )

    receipt = freeze_receipt
    if receipt is None:
        default = default_receipt(evidence)
        if default.is_file():
            receipt = load_receipt(default, policy_sha256)
    checkpoint_sha = ""
    if receipt is not None:
        try:
            checkpoint = verify_editorial_approved_checkpoint(
                Path(receipt.prerequisite_checkpoint_path)
            )
            del checkpoint
            checkpoint_sha = sha256_file(Path(receipt.prerequisite_checkpoint_path))
        except (PrerequisiteError, OSError) as error:
            raise GateP2Error("prerequisite-stale", str(error)) from error
    if drive:
        bins = _media_bins()
        observations = drive_all_fixtures(
            evidence,
            connection,
            checkpoint_sha,
            ffmpeg_bin=bins[0],
            ffprobe_bin=bins[1],
        )
    if observations is None:
        raise GateP2Error("evidence-incomplete", "no observations supplied")
    state, _checkpoint = check_all(policy, receipt, evidence, observations)
    marker: Phase2ExitMarker | None = None
    if all(state.criteria.values()):
        exit_marker(evidence, policy_sha256, observations)
        raw = (evidence / EXIT_MARKER_NAME).read_bytes()
        marker = Phase2ExitMarker.model_validate_json(raw)
    result = build_result(policy, policy_sha256, evidence, state)
    result_path = evidence / RESULT_NAME
    atomic_write(result_path, canonical_gate_bytes(result))
    return Phase2GateOutcome(
        result=result,
        result_path=result_path,
        exit_marker=marker,
        mismatches=tuple((row.code, row.detail) for row in state.mismatches),
    )


def run_phase2_gate(arguments: argparse.Namespace) -> int:
    try:
        policy, policy_sha256 = load_policy(arguments.policy)
        receipt_path = (
            arguments.freeze_receipt
            if arguments.freeze_receipt is not None
            else default_receipt(arguments.evidence)
        )
        receipt = load_receipt(receipt_path, policy_sha256)
    except (OSError, ValidationError, GateP2Error) as error:
        print(f"{MARKER} FAIL code=gate-inputs {error}", file=sys.stderr)
        return EXIT_USAGE
    stopped = stop_recorded(arguments.evidence)
    if stopped is not None:
        print(
            f"{MARKER} STOP-REFUSED gate={stopped.gate_id} "
            f"criterion={stopped.criterion_id} reason={stopped.reason}"
        )
        return EXIT_STOP
    if not arguments.offline:
        try:
            connection = resolve_connection(arguments.evidence)
        except (GateP2Error, BridgeConnectionError) as error:
            print(f"{MARKER} BLOCKED code=resolve-unavailable {error}", file=sys.stderr)
            return EXIT_FAIL
    else:
        connection = None
    arguments.evidence.mkdir(parents=True, exist_ok=True)
    try:
        outcome = evaluate(
            policy,
            policy_sha256,
            arguments.evidence,
            freeze_receipt=receipt,
            connection=connection,
        )
    except (OSError, ValidationError, GateP2Error) as error:
        print(f"{MARKER} FAIL code=drive-error {error}", file=sys.stderr)
        return EXIT_FAIL
    for row in outcome.result.criteria_results:
        print(f"{MARKER} criterion {row.criterion_id} {'PASS' if row.passed else 'FAIL'}")
    for code, detail in outcome.mismatches:
        print(f"{MARKER} mismatch code={code} detail={detail}")
    verdict = "PASS" if outcome.result.passed else "FAIL"
    print(f"{MARKER} {verdict} gate-result={outcome.result_path}")
    if not outcome.result.passed or outcome.exit_marker is None:
        return EXIT_FAIL
    print(
        f"{MARKER} next_checkpoint=PHASE_2_EXIT "
        "fixture_final_records=fixture_only publication_decisions=false"
    )
    print(f"{MARKER} exit-marker={arguments.evidence / EXIT_MARKER_NAME}")
    return EXIT_PASS


def parser() -> argparse.ArgumentParser:
    entry = argparse.ArgumentParser(prog="run_gate phase-2")
    entry.add_argument("--policy", type=Path, required=True)
    entry.add_argument("--evidence", type=Path, required=True)
    entry.add_argument("--freeze-receipt", type=Path, default=None)
    entry.add_argument(
        "--offline",
        action="store_true",
        help="evaluate supplied observations without driving (test harness only)",
    )
    return entry


__all__ = [
    "EXIT_FAIL",
    "EXIT_PASS",
    "EXIT_STOP",
    "EXIT_USAGE",
    "MARKER",
    "Phase2GateOutcome",
    "evaluate",
    "load_policy",
    "load_receipt",
    "parser",
    "run_phase2_gate",
]
