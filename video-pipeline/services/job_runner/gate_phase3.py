"""The Phase-3 A/B Profile-swap Gate entry (Todo 62).

Loads the frozen policy (canonical), refuses while a cross-gate stop marker
is present, drives the REAL A/B swap live (the same approved fixture
Timeline IR built and rendered under snapshot A then snapshot B through the
bridge), reruns the COMPLETE Phase-2 gate live into its own evidence
subtree, statically inspects the Builder surface, recomputes every frozen
criterion from raw evidence, and writes the canonical ``gate-result.json``.
Exit codes: 0 pass, 1 fail, 2 usage/policy error, 3 stop. The gate never
passes on Manual Finalization or Phase-4 dependencies — those are typed
failures.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.foundation_io import atomic_write, canonical_model_bytes
from services.gates import GatePolicy, GateResult
from services.gates.serialization import canonical_gate_bytes
from services.job_runner.gate_p3_ab import compile_ab, write_ab_evidence
from services.job_runner.gate_p3_checks import check_all
from services.job_runner.gate_p3_live import P3LiveDriver
from services.job_runner.gate_p3_models import (
    AB_DIR_NAME,
    BUILDS_DIR_NAME,
    OBSERVATION_NAME,
    P3GateObservation,
    P3ScanRecord,
)
from services.job_runner.gate_p3_regression import (
    DEFAULT_TIMEOUT_SECONDS,
    default_receipt,
    host_report_path,
    run_regression,
)
from services.job_runner.gate_p3_result import build_result, write_result
from services.job_runner.gate_p3_scan import (
    default_channel_roots,
    loaded_module_names,
    repo_root,
    scan_channel_branches,
    scan_phase4_imports,
)
from services.resolve_bridge.connection import BridgeConnectionError, ResolveConnection
from services.resolve_bridge.launch import launch_and_connect
from services.resolve_bridge.readiness import load_host_report
from services.spike.stop_rules import stop_recorded
from services.toolchain.models import load_lock

MARKER: Final = "phase3-gate"
EXIT_PASS: Final = 0
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 2
EXIT_STOP: Final = 3
DEFAULT_HOST_REPORT: Final = "resolve-host.json"


class GateP3Error(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Phase3GateOutcome:
    result: GateResult
    result_path: Path
    mismatches: tuple[tuple[str, str], ...]


def load_policy(path: Path) -> tuple[GatePolicy, str]:
    raw = path.read_bytes()
    policy = GatePolicy.model_validate_json(raw)
    if raw != canonical_gate_bytes(policy):
        raise GateP3Error("policy-noncanonical", str(path))
    return policy, hashlib.sha256(raw).hexdigest()


def resolve_connection(evidence: Path) -> ResolveConnection:
    report = host_report_path(evidence)
    if report is None:
        raise GateP3Error(
            "resolve-unavailable",
            "no resolve host report (set RESOLVE_HOST_REPORT or place "
            f"{DEFAULT_HOST_REPORT} beside the evidence dir)",
        )
    return launch_and_connect(load_host_report(report))


def _media_bins() -> tuple[Path, Path]:
    lock = load_lock(repo_root() / "config/toolchains/phase-3-v1.json")
    return Path(lock.ffmpeg.ffmpeg.path), Path(lock.ffmpeg.ffprobe.path)


def _scan_record() -> P3ScanRecord:
    roots = default_channel_roots()
    return P3ScanRecord(
        channel_roots=tuple(path.as_posix() for path in roots),
        channel_findings=scan_channel_branches(roots),
        phase4_modules=scan_phase4_imports(loaded_module_names()),
    )


def evaluate(  # noqa: PLR0913 (gate contract: policy + evidence + drive seams)
    policy: GatePolicy,
    policy_sha256: str,
    evidence: Path,
    *,
    connection: ResolveConnection | None = None,
    host_report: Path | None = None,
    receipt: Path | None = None,
    regression_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    drive: bool = True,
    observation: P3GateObservation | None = None,
) -> Phase3GateOutcome:
    if drive:
        if connection is None:
            raise GateP3Error("resolve-unavailable", "the live A/B drive requires the bridge")
        ffmpeg, ffprobe = _media_bins()
        plan = compile_ab()
        write_ab_evidence(evidence / AB_DIR_NAME, plan)
        driver = P3LiveDriver(
            connection=connection,
            plan=plan,
            builds_root=evidence / BUILDS_DIR_NAME,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            render_deadline=900.0,
        )
        builds = driver.drive()
        for build in builds:
            atomic_write(
                Path(build.work_dir) / OBSERVATION_NAME, canonical_model_bytes(build)
            )
        report = host_report or host_report_path(evidence)
        if report is None:
            raise GateP3Error("resolve-unavailable", "the regression rerun needs the host report")
        regression = run_regression(
            evidence,
            host_report=report,
            receipt=receipt or default_receipt(evidence),
            timeout_seconds=regression_timeout,
        )
        record = _scan_record()
        observation = P3GateObservation(builds=builds, regression=regression, scan=record)
        atomic_write(evidence / OBSERVATION_NAME, canonical_model_bytes(observation))
    if observation is None:
        raise GateP3Error("evidence-incomplete", "no observation supplied")
    state = check_all(policy, evidence, observation)
    result = build_result(policy, policy_sha256, evidence, state)
    result_path = write_result(evidence, result)
    return Phase3GateOutcome(
        result=result,
        result_path=result_path,
        mismatches=tuple((row.code, row.detail) for row in state.mismatches),
    )


def run_phase3_gate(arguments: argparse.Namespace) -> int:
    try:
        policy, policy_sha256 = load_policy(arguments.policy)
    except (OSError, ValidationError, GateP3Error) as error:
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
        except (GateP3Error, BridgeConnectionError) as error:
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
            connection=connection,
            receipt=arguments.freeze_receipt,
            regression_timeout=arguments.regression_timeout,
            drive=not arguments.offline,
        )
    except (OSError, ValidationError, GateP3Error) as error:
        print(f"{MARKER} FAIL code=drive-error {error}", file=sys.stderr)
        return EXIT_FAIL
    for row in outcome.result.criteria_results:
        print(f"{MARKER} criterion {row.criterion_id} {'PASS' if row.passed else 'FAIL'}")
    for code, detail in outcome.mismatches:
        print(f"{MARKER} mismatch code={code} detail={detail}")
    verdict = "PASS" if outcome.result.passed else "FAIL"
    print(f"{MARKER} {verdict} gate-result={outcome.result_path}")
    if not outcome.result.passed:
        return EXIT_FAIL
    print(
        f"{MARKER} profile_swap=A/B structural_drift=0 "
        "manual_finalization=false phase4_dependency=false"
    )
    return EXIT_PASS


def parser() -> argparse.ArgumentParser:
    entry = argparse.ArgumentParser(prog="run_gate phase-3")
    entry.add_argument("--policy", type=Path, required=True)
    entry.add_argument("--evidence", type=Path, required=True)
    entry.add_argument("--freeze-receipt", type=Path, default=None)
    entry.add_argument("--regression-timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    entry.add_argument(
        "--offline",
        action="store_true",
        help="evaluate the supplied observation without driving (test harness only)",
    )
    return entry


__all__ = [
    "EXIT_FAIL",
    "EXIT_PASS",
    "EXIT_STOP",
    "EXIT_USAGE",
    "MARKER",
    "GateP3Error",
    "Phase3GateOutcome",
    "evaluate",
    "load_policy",
    "parser",
    "resolve_connection",
    "run_phase3_gate",
]
