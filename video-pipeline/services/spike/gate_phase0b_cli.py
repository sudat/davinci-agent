"""Phase-0B exit-gate CLI (dispatched from ``services.spike.run_gate``).

``run_gate phase-0b --policy <frozen policy> --evidence <dir>`` verifies the
policy bindings, refuses to start while a stop marker is recorded, drives the
six-variant evidence matrix inside one live Resolve session, re-derives the
capability findings, recomputes all five frozen criteria from raw evidence,
and writes ``gate-result.json``. Exit codes match the 0A gate: 0 pass, 1
fail, 2 usage/fault, 3 stop (recorded or triggered), 4 live bridge
unavailable.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.gates import GatePolicy
from services.gates.serialization import canonical_gate_bytes
from services.resolve_bridge.readiness import HostReadinessError, load_host_report
from services.spike.gate_models import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_STOP,
    EXIT_UNAVAILABLE,
)
from services.spike.gate_phase0b_driver import (
    DRIVER_ERRORS,
    Gate0bBindingError,
    Gate0bUnavailableError,
    PolicyRefs,
    drive_phase0b,
    preflight_policy_bindings,
)
from services.spike.gate_phase0b_evaluate import (
    Evaluate0bInputs,
    GoldenLoadError,
    evaluate0b,
    write_result,
)
from services.spike.gate_phase0b_matrix import derive0b_matrix, write0b_matrix
from services.spike.gate_phase0b_models import (
    DEFAULT_LOCK,
    GATE0B_MARKER,
    PARENT_DIR,
    RESULT_NAME,
    SYNC_NAME,
)
from services.spike.stop_rules import record_stop, stop_recorded

BOOTSTRAP_BIN: Final = "bootstrap/ffmpeg-7.1.1/bin"


def _resolve_host_report(arguments: argparse.Namespace) -> Path | None:
    if arguments.host_report is not None:
        return arguments.host_report
    from_env = os.environ.get("RESOLVE_HOST_REPORT")
    if from_env:
        return Path(from_env)
    fallback = arguments.evidence.parent / "resolve-host.json"
    return fallback if fallback.is_file() else None


def run_cli(arguments: argparse.Namespace) -> int:
    evidence = arguments.evidence
    try:
        policy_raw = arguments.policy.read_bytes()
        policy = GatePolicy.model_validate_json(policy_raw)
    except (OSError, ValidationError) as error:
        print(f"{GATE0B_MARKER} FAIL code=policy-invalid {error}", file=sys.stderr)
        return EXIT_FAIL
    if policy_raw != canonical_gate_bytes(policy):
        print(f"{GATE0B_MARKER} FAIL code=policy-noncanonical", file=sys.stderr)
        return EXIT_FAIL
    policy_sha256 = hashlib.sha256(policy_raw).hexdigest()

    stopped = stop_recorded(evidence)
    if stopped is not None:
        print(
            f"{GATE0B_MARKER} STOP-REFUSED gate={stopped.gate_id} "
            f"criterion={stopped.criterion_id} reason={stopped.reason}"
        )
        return EXIT_STOP

    host_report = _resolve_host_report(arguments)
    if host_report is None or not host_report.is_file():
        print(
            f"{GATE0B_MARKER} needs-live reason=no host report "
            "(--host-report, RESOLVE_HOST_REPORT, or <evidence>/../resolve-host.json)",
            file=sys.stderr,
        )
        return EXIT_UNAVAILABLE
    lock_path = arguments.lock or DEFAULT_LOCK
    ffmpeg = arguments.ffmpeg or host_report.parent / BOOTSTRAP_BIN / "ffmpeg"
    ffprobe = arguments.ffprobe or host_report.parent / BOOTSTRAP_BIN / "ffprobe"
    parent_result = arguments.parent_result or (host_report.parent / PARENT_DIR / RESULT_NAME)
    budget = arguments.budget
    print(f"{GATE0B_MARKER} policy sha256={policy_sha256}")
    print(f"{GATE0B_MARKER} host-report {host_report}")

    try:
        preflight_policy_bindings(
            lock_path,
            PolicyRefs(
                fixture_manifest_sha256=policy.fixture_manifest_sha256 or "",
                toolchain_lock_sha256=policy.toolchain_lock_sha256 or "",
                golden_sha256=policy.golden_sha256 or "",
            ),
        )
        drive_phase0b(evidence, host_report, lock_path, ffmpeg, ffprobe, budget)
    except Gate0bUnavailableError as error:
        print(f"{GATE0B_MARKER} needs-live reason={error}")
        return EXIT_UNAVAILABLE
    except Gate0bBindingError as error:
        print(f"{GATE0B_MARKER} FAIL code=policy-binding-drift {error}", file=sys.stderr)
        return EXIT_FAIL
    except DRIVER_ERRORS as error:
        print(f"{GATE0B_MARKER} FAIL code=drive-error {error}", file=sys.stderr)
        return EXIT_FAIL
    except HostReadinessError as error:
        print(f"{GATE0B_MARKER} FAIL code=drive-inputs {error}", file=sys.stderr)
        return EXIT_FAIL

    try:
        host = load_host_report(host_report)
        matrix = derive0b_matrix(evidence, host.application.version, host.application.build)
        write0b_matrix(matrix, evidence, arguments.capabilities_dir)
        outcome = evaluate0b(
            Evaluate0bInputs(
                policy=policy,
                policy_sha256=policy_sha256,
                evidence=evidence,
                host_report_path=host_report,
                parent_result_path=parent_result,
            )
        )
    except GoldenLoadError as error:
        print(f"{GATE0B_MARKER} FAIL code=golden-unbound {error}", file=sys.stderr)
        return EXIT_FAIL
    except (OSError, ValidationError) as error:
        print(f"{GATE0B_MARKER} FAIL code=evaluate-inputs {error}", file=sys.stderr)
        return EXIT_FAIL

    result_path = write_result(outcome, evidence)
    passed_count = sum(1 for row in outcome.result.criteria_results if row.passed)
    print(f"{GATE0B_MARKER} criteria {passed_count}/{len(outcome.result.criteria_results)} passed")
    print(f"{GATE0B_MARKER} sync-measurements {evidence / SYNC_NAME}")
    for variant in outcome.variants_passed:
        print(f"{GATE0B_MARKER} variant {variant} PASS")
    for row in outcome.mismatches:
        print(f"{GATE0B_MARKER} mismatch code={row.code} detail={row.detail}")
    if outcome.stop_triggered:
        marker = record_stop(
            evidence, outcome.stop_criterion, outcome.stop_reason, gate_id="phase-0b"
        )
        print(f"{GATE0B_MARKER} STOP code={outcome.stop_criterion} reason={outcome.stop_reason}")
        print(f"{GATE0B_MARKER} stop-marker={marker}")
        return EXIT_STOP
    if not outcome.result.passed:
        print(f"{GATE0B_MARKER} FAIL gate-result={result_path}")
        return EXIT_FAIL
    print(f"{GATE0B_MARKER} PASS gate-result={result_path}")
    return EXIT_PASS
