"""Control Plane Baseline gate runner CLI (Todo 13).

``run_gate control-plane-baseline --policy <frozen policy> --evidence <dir>``
drives the six frozen Control Plane fixture scenarios plus the
approval-ingress checks against the REAL Todos 7-12 components, then
recomputes every frozen criterion from raw evidence and writes
``<evidence>/gate-result.json``. Exit codes: 0 pass, 1 fail, 2
usage/policy error. ``--tty-fixture`` additionally exercises the TTY
display/confirm path over a real pty while still creating only
FIXTURE-MARKED records (never ``fixture_only=false``).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.foundation_io import canonical_model_bytes
from services.gates import GatePolicy
from services.gates.serialization import canonical_gate_bytes
from services.job_runner.gate_cp_approvals import drive_approvals
from services.job_runner.gate_cp_evaluate import evaluate_gate
from services.job_runner.gate_cp_scenarios import (
    MANIFEST_DIR,
    drive_scenarios,
)

MARKER: Final = "cp-gate:"
EXIT_PASS: Final = 0
EXIT_FAIL: Final = 1
EXIT_USAGE: Final = 2
DEFAULT_TOOLCHAIN_LOCK: Final = Path("config/toolchains/phase-0c-v1.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_gate")
    parser.add_argument(
        "gate", choices=("control-plane-baseline", "phase-1-technical", "phase-2")
    )
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--tty-fixture", action="store_true")
    parser.add_argument("--parent-result", type=Path)
    parser.add_argument("--freeze-receipt", type=Path)
    parser.add_argument("--manifest-dir", type=Path, default=MANIFEST_DIR)
    parser.add_argument("--toolchain-lock", type=Path, default=DEFAULT_TOOLCHAIN_LOCK)
    parser.add_argument("--work-id", default=None, help="phase-1-technical only")
    parser.add_argument("--offline", action="store_true", help="phase-2 only")
    return parser


def _load_policy(path: Path) -> tuple[GatePolicy, str] | None:
    try:
        raw = path.read_bytes()
        policy = GatePolicy.model_validate_json(raw)
    except (OSError, ValidationError) as error:
        print(f"{MARKER} FAIL code=policy-invalid {error}", file=sys.stderr)
        return None
    if raw != canonical_gate_bytes(policy):
        print(f"{MARKER} FAIL code=policy-noncanonical", file=sys.stderr)
        return None
    return policy, hashlib.sha256(raw).hexdigest()


def _check_freeze_receipt(path: Path, policy: GatePolicy, policy_sha256: str) -> bool:
    from services.fixtures.models import ControlPlaneFreezeReceipt  # noqa: PLC0415

    try:
        raw = path.read_bytes()
        receipt = ControlPlaneFreezeReceipt.model_validate_json(raw)
    except (OSError, ValidationError):
        print(f"{MARKER} FAIL code=freeze-receipt-invalid", file=sys.stderr)
        return False
    if raw != canonical_model_bytes(receipt):
        print(f"{MARKER} FAIL code=freeze-receipt-noncanonical", file=sys.stderr)
        return False
    parents = tuple(policy.parent_gate_result_hashes)
    bindings_ok = (
        receipt.policy_sha256 == policy_sha256
        and receipt.fixture_manifests_combined_sha256 == policy.fixture_manifest_sha256
        and receipt.toolchain_lock_sha256 == policy.toolchain_lock_sha256
        and len(parents) == 1
        and receipt.parent_gate_result_sha256 == parents[0]
    )
    if not bindings_ok:
        print(f"{MARKER} FAIL code=freeze-receipt-binding-drift", file=sys.stderr)
        return False
    return True


def _resolve_parent(arguments: argparse.Namespace) -> Path | None:
    if arguments.parent_result is not None:
        return arguments.parent_result
    fallback = arguments.evidence.parent / "phase-0c" / "gate-result.json"
    return fallback if fallback.is_file() else None


def run_gate(arguments: argparse.Namespace) -> int:
    loaded = _load_policy(arguments.policy)
    if loaded is None:
        return EXIT_USAGE
    policy, policy_sha256 = loaded
    if (
        arguments.freeze_receipt is not None
        and not _check_freeze_receipt(arguments.freeze_receipt, policy, policy_sha256)
    ):
        return EXIT_USAGE
    parent_result = _resolve_parent(arguments)
    if parent_result is None:
        print(
            f"{MARKER} FAIL code=parent-unresolvable "
            "(pass --parent-result or place phase-0c/gate-result.json beside the evidence dir)",
            file=sys.stderr,
        )
        return EXIT_FAIL
    print(f"{MARKER} policy sha256={policy_sha256}")
    print(f"{MARKER} parent-result {parent_result}")
    arguments.evidence.mkdir(parents=True, exist_ok=True)
    drive_scenarios(arguments.evidence, arguments.manifest_dir)
    drive_approvals(arguments.evidence, tty_fixture=arguments.tty_fixture)
    outcome = evaluate_gate(
        policy,
        policy_sha256=policy_sha256,
        evidence=arguments.evidence,
        manifest_dir=arguments.manifest_dir,
        toolchain_lock=arguments.toolchain_lock,
        parent_result=parent_result,
    )
    for row in outcome.result.criteria_results:
        print(f"{MARKER} criterion {row.criterion_id} {'PASS' if row.passed else 'FAIL'}")
    for mismatch in outcome.mismatches:
        print(f"{MARKER} mismatch code={mismatch.code} detail={mismatch.detail}")
    verdict = "PASS" if outcome.result.passed else "FAIL"
    print(f"{MARKER} {verdict} gate-result={outcome.result_path}")
    if not outcome.result.passed:
        return EXIT_FAIL
    return EXIT_PASS


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.gate == "phase-1-technical":
        import os  # noqa: PLC0415

        fault_fixture = os.environ.get("QA_FAULT_FIXTURE")
        if fault_fixture is not None:
            from services.job_runner import gate_p1_faults  # noqa: PLC0415

            return gate_p1_faults.run_fault_cli(Path(fault_fixture), arguments.policy)
        from services.job_runner import gate_phase1  # noqa: PLC0415

        gate_arguments = gate_phase1.parser().parse_args(
            [
                "--policy",
                str(arguments.policy),
                "--evidence",
                str(arguments.evidence),
                *(["--work-id", arguments.work_id] if arguments.work_id else []),
            ]
        )
        return gate_phase1.run_phase1_gate(gate_arguments)
    if arguments.gate == "phase-2":
        import os  # noqa: PLC0415

        fault_fixture = os.environ.get("QA_FAULT_FIXTURE")
        if fault_fixture is not None:
            from services.job_runner import gate_p2_faults  # noqa: PLC0415

            return gate_p2_faults.run_fault_cli(Path(fault_fixture), arguments.policy)
        from services.job_runner import gate_phase2  # noqa: PLC0415

        gate_arguments = gate_phase2.parser().parse_args(
            [
                "--policy",
                str(arguments.policy),
                "--evidence",
                str(arguments.evidence),
                *(
                    ["--freeze-receipt", str(arguments.freeze_receipt)]
                    if arguments.freeze_receipt
                    else []
                ),
                *(["--offline"] if arguments.offline else []),
            ]
        )
        return gate_phase2.run_phase2_gate(gate_arguments)
    return run_gate(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
