"""Phase-0A exit-gate runner CLI.

``run_gate phase-0a --policy <frozen policy> --evidence <dir>`` drives the
full live evidence matrix (six bound spike runs around a bounded Resolve
restart, failed-partial-build recovery, capability probes), derives the
capability matrix from recomputed evidence, re-evaluates every frozen
criterion from raw evidence, and writes ``gate-result.json``. Exit codes:
0 pass, 1 fail, 2 usage/fault, 3 stop (recorded or triggered),
4 live bridge unavailable.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.contracts.build_report import BuildReport0A
from services.fixtures.manifest import Phase0AFixtureManifest
from services.gates import GatePolicy
from services.gates.serialization import canonical_gate_bytes
from services.resolve_bridge.base_cut_plan import BaseCutError
from services.resolve_bridge.fixed_presentation_tools import MediaTools, RenderError
from services.resolve_bridge.readiness import HostReadinessError
from services.spike.gate_driver import GateDriverError, GateUnavailableError, drive
from services.spike.gate_evaluate import (
    GateEvaluationInputs,
    GateOutcome,
    evaluate,
    write_result,
)
from services.spike.gate_matrix import derive_matrix, write_matrix
from services.spike.gate_models import (
    DEFAULT_BUDGET_SECONDS,
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_STOP,
    EXIT_UNAVAILABLE,
    MARKER,
    MATRIX_NAME,
    PROBES_DIR,
    PROBES_NAME,
    RUN_COUNT,
    CapabilityProbes,
    run_report_path,
)
from services.spike.gate_probes import ProbeError
from services.spike.restart import RestartError
from services.spike.stop_rules import record_stop, stop_recorded

BOOTSTRAP_BIN: Final = "bootstrap/ffmpeg-7.1.1/bin"
DEFAULT_MANIFEST: Final = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_gate")
    parser.add_argument("gate", choices=("phase-0a", "phase-0b"))
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--host-report", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--ffprobe", type=Path)
    parser.add_argument("--capabilities-dir", type=Path, default=Path("capabilities"))
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--parent-result", dest="parent_result", type=Path)
    return parser


def _resolve_host_report(arguments: argparse.Namespace) -> Path | None:
    if arguments.host_report is not None:
        return arguments.host_report
    from_env = os.environ.get("RESOLVE_HOST_REPORT")
    if from_env:
        return Path(from_env)
    fallback = arguments.evidence.parent / "resolve-host.json"
    return fallback if fallback.is_file() else None


def _summary_lines(outcome: GateOutcome, result_path: Path) -> list[str]:
    passed_count = sum(1 for row in outcome.result.criteria_results if row.passed)
    lines = [
        f"{MARKER} criteria {passed_count}/{len(outcome.result.criteria_results)} passed",
        f"{MARKER} expected-fingerprint={outcome.expected_fingerprint}",
        *(
            f"{MARKER} run-{index} fingerprint={fingerprint}"
            for index, fingerprint in enumerate(outcome.run_fingerprints, start=1)
        ),
    ]
    lines.extend(
        f"{MARKER} mismatch code={row.code} detail={row.detail}" for row in outcome.mismatches
    )
    verdict = "PASS" if outcome.result.passed else "FAIL"
    lines.append(f"{MARKER} {verdict} gate-result={result_path}")
    return lines


def main() -> int:
    arguments = _parser().parse_args()
    fault_fixture = os.environ.get("QA_FAULT_FIXTURE")
    if fault_fixture is not None:
        if arguments.gate == "phase-0b":
            from services.spike import gate_phase0b_faults  # noqa: PLC0415

            return gate_phase0b_faults.run_fault_cli(
                Path(fault_fixture), arguments.policy
            )
        from services.spike import gate_faults  # noqa: PLC0415

        return gate_faults.run_fault_cli(
            Path(fault_fixture), arguments.policy, arguments.manifest
        )
    if arguments.evidence is None:
        print("--evidence is required outside fault mode", file=sys.stderr)
        return 2
    if arguments.gate == "phase-0b":
        from services.spike import gate_phase0b_cli  # noqa: PLC0415

        return gate_phase0b_cli.run_cli(arguments)

    try:
        policy_raw = arguments.policy.read_bytes()
        policy = GatePolicy.model_validate_json(policy_raw)
    except (OSError, ValidationError) as error:
        print(f"{MARKER} FAIL code=policy-invalid {error}", file=sys.stderr)
        return EXIT_FAIL
    if policy_raw != canonical_gate_bytes(policy):
        print(f"{MARKER} FAIL code=policy-noncanonical", file=sys.stderr)
        return EXIT_FAIL
    policy_sha256 = hashlib.sha256(policy_raw).hexdigest()

    stopped = stop_recorded(arguments.evidence)
    if stopped is not None:
        print(
            f"{MARKER} STOP-REFUSED gate={stopped.gate_id} "
            f"criterion={stopped.criterion_id} reason={stopped.reason}"
        )
        return EXIT_STOP

    host_report = _resolve_host_report(arguments)
    if host_report is None or not host_report.is_file():
        print(
            f"{MARKER} needs-live reason=no host report "
            "(--host-report, RESOLVE_HOST_REPORT, or <evidence>/../resolve-host.json)",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE
    fixture_dir = arguments.fixture_dir or (host_report.parent / "phase-0a" / "fixture")
    ffmpeg = arguments.ffmpeg or (host_report.parent / BOOTSTRAP_BIN / "ffmpeg")
    ffprobe = arguments.ffprobe or (host_report.parent / BOOTSTRAP_BIN / "ffprobe")
    print(f"{MARKER} policy sha256={policy_sha256}")
    print(f"{MARKER} host-report {host_report}")

    try:
        drive_result = drive(
            arguments.evidence,
            host_report,
            arguments.manifest,
            fixture_dir,
            ffmpeg,
            ffprobe,
            arguments.budget,
        )
    except GateUnavailableError as error:
        print(f"{MARKER} needs-live reason={error}")
        return EXIT_UNAVAILABLE
    except (GateDriverError, RestartError, ProbeError, BaseCutError, RenderError) as error:
        print(f"{MARKER} FAIL code=drive-error {error}", file=sys.stderr)
        return EXIT_FAIL
    except (HostReadinessError, ValidationError, OSError) as error:
        print(f"{MARKER} FAIL code=drive-inputs {error}", file=sys.stderr)
        return EXIT_FAIL

    manifest = Phase0AFixtureManifest.model_validate_json(arguments.manifest.read_bytes())
    reports: dict[int, BuildReport0A] = {}
    for index in range(1, RUN_COUNT + 1):
        reports[index] = BuildReport0A.model_validate_json(
            run_report_path(arguments.evidence, index).read_bytes()
        )
    probes = CapabilityProbes.model_validate_json(
        (arguments.evidence / PROBES_DIR / PROBES_NAME).read_bytes()
    )
    matrix = derive_matrix(manifest, arguments.evidence, reports, probes)
    write_matrix(matrix, arguments.evidence, arguments.capabilities_dir)
    print(
        f"{MARKER} capabilities entries={len(matrix.capabilities)} "
        f"findings={len(matrix.findings)} matrix={arguments.evidence / MATRIX_NAME}"
    )
    print(
        f"{MARKER} restart evidence={arguments.evidence / 'restart' / 'restart-evidence.json'}"
    )
    print(
        f"{MARKER} recovery partial={drive_result.partial_project_name} "
        f"rebuild={drive_result.rebuild_project_name} reused=false"
    )

    outcome = evaluate(
        GateEvaluationInputs(
            policy=policy,
            policy_sha256=policy_sha256,
            evidence=arguments.evidence,
            manifest_path=arguments.manifest,
            host_report_path=host_report,
            fixture_dir=fixture_dir,
            ffmpeg_bin=ffmpeg,
            ffprobe_bin=ffprobe,
            port=MediaTools(ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe),
        )
    )
    result_path = write_result(outcome, arguments.evidence)
    for line in _summary_lines(outcome, result_path):
        print(line)
    if outcome.stop_triggered:
        marker = record_stop(arguments.evidence, outcome.stop_criterion, outcome.stop_reason)
        print(f"{MARKER} STOP code={outcome.stop_criterion} reason={outcome.stop_reason}")
        print(f"{MARKER} stop-marker={marker}")
        return EXIT_STOP
    if not outcome.result.passed:
        return EXIT_FAIL
    return EXIT_PASS


if __name__ == "__main__":
    raise SystemExit(main())
