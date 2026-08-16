"""Offline fault scenarios for the Phase-0B gate (``QA_FAULT_FIXTURE`` mode).

Each fault synthesizes a complete fake evidence tree with one injected defect,
runs the real 0B evaluator over it, and prints the observation. Exit code is
``EXIT_FAULT`` whether or not the fault was detected; the printed observation
discriminates. Stop faults additionally write the shared stop marker and
demonstrate that a rerun is refused while it is present.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Final

from services.contracts.primitives import StrictModel
from services.foundation_io import sha256_file
from services.gates import GatePolicy
from services.spike.gate_models import EXIT_FAULT
from services.spike.gate_phase0b_driver import PolicyRefs, preflight_policy_bindings
from services.spike.gate_phase0b_evaluate import Evaluate0bInputs, evaluate0b
from services.spike.gate_phase0b_fakes import FaultKnobs0b, refusal_label, synthesize0b
from services.spike.gate_phase0b_models import (
    DEFAULT_LOCK,
    GATE0B_MARKER,
    PARENT_DIR,
    RESULT_NAME,
    STOP_STALE_BINDING,
    fixture_manifest_path,
)
from services.spike.stop_rules import (
    load_stop,
    record_stop,
    stop_marker_path,
    stop_recorded,
)

FAULT_KINDS: Final = {
    "off_by_one_golden": "off-by-one-golden",
    "missing_duplicate_report": "missing-duplicate-report",
    "sync_over_one_frame": "sync-over-one-frame",
    "stale_resolve_build": "stale-prior-build-evidence",
    "vfr_original_edit": "original-refused-for-resolve-edit",
}


class Gate0bFaultSpec(StrictModel):
    fault: str


def _knobs(fault: str) -> FaultKnobs0b:
    return FaultKnobs0b(
        off_by_one_golden=fault == "off_by_one_golden",
        missing_duplicate_report=fault == "missing_duplicate_report",
        sync_over_one_frame=fault == "sync_over_one_frame",
        stale_resolve_build=fault == "stale_resolve_build",
    )


def run_fault_cli(spec_path: Path, policy_path: Path) -> int:
    try:
        spec = Gate0bFaultSpec.model_validate_json(spec_path.read_bytes())
        policy = GatePolicy.model_validate_json(policy_path.read_bytes())
    except (OSError, ValueError) as error:
        print(f"{GATE0B_MARKER} fault-fixture invalid: {error}", file=sys.stderr)
        return EXIT_FAULT
    if spec.fault not in FAULT_KINDS:
        print(f"{GATE0B_MARKER} fault-fixture invalid: unknown fault {spec.fault!r}")
        return EXIT_FAULT
    try:
        preflight_policy_bindings(
            DEFAULT_LOCK,
            PolicyRefs(
                fixture_manifest_sha256=policy.fixture_manifest_sha256 or "",
                toolchain_lock_sha256=policy.toolchain_lock_sha256 or "",
                golden_sha256=policy.golden_sha256 or "",
            ),
        )
    except (OSError, ValueError) as error:
        print(f"{GATE0B_MARKER} fault-fixture invalid: {error}", file=sys.stderr)
        return EXIT_FAULT

    if spec.fault == "vfr_original_edit":
        from services.spike.gate_phase0b_fakes import load_world  # noqa: PLC0415

        fixture_path = fixture_manifest_path("p0b-vfr-2-3-cadence")
        from services.fixtures.manifest_phase0b import Phase0BFixtureManifest  # noqa: PLC0415

        fixture = Phase0BFixtureManifest.model_validate_json(fixture_path.read_bytes())
        world = load_world(fixture)
        original = Path(world.manifest.file.path)
        label = refusal_label(world.record, original)
        print(f"{GATE0B_MARKER} fault-observed=true code={label}")
        return EXIT_FAULT

    with tempfile.TemporaryDirectory(prefix="gate0b-fault-") as scratch:
        evidence = Path(scratch) / "phase-0b"
        host_report = synthesize0b(evidence, _knobs(spec.fault))
        parent = _parent_result()
        outcome = evaluate0b(
            Evaluate0bInputs(
                policy=policy,
                policy_sha256=sha256_file(policy_path),
                evidence=evidence,
                host_report_path=host_report,
                parent_result_path=parent,
            )
        )
        for row in outcome.mismatches:
            print(
                f"{GATE0B_MARKER} mismatch code={row.code} detail={row.detail}",
                file=sys.stderr,
            )
        expected_code = FAULT_KINDS[spec.fault]
        if outcome.stop_triggered:
            print(
                f"{GATE0B_MARKER} STOP code={outcome.stop_criterion} "
                f"reason={outcome.stop_reason}"
            )
            marker = record_stop(
                evidence, outcome.stop_criterion, outcome.stop_reason, gate_id="phase-0b"
            )
            refused = stop_recorded(evidence)
            loaded = load_stop(stop_marker_path(evidence))
            print(f"{GATE0B_MARKER} stop-marker-written path={marker.name}")
            print(
                f"{GATE0B_MARKER} rerun-refused={str(refused is not None).lower()} "
                f"criterion={loaded.criterion_id}"
            )
            if outcome.stop_criterion != STOP_STALE_BINDING:
                print(
                    f"ERROR: expected stop {STOP_STALE_BINDING} but got {outcome.stop_criterion}",
                    file=sys.stderr,
                )
                return EXIT_FAULT
        else:
            observed = expected_code in {row.code for row in outcome.mismatches}
            print(f"{GATE0B_MARKER} fault-observed={str(observed).lower()} code={expected_code}")
            if not observed:
                print(f"ERROR: fault {spec.fault!r} not observed", file=sys.stderr)
    return EXIT_FAULT


def _parent_result() -> Path:
    """Locate the real frozen phase-0a result that the policy binds."""

    override = os.environ.get("PHASE0A_RESULT")
    if override:
        return Path(override)
    host = os.environ.get("RESOLVE_HOST_REPORT")
    if host:
        candidate = Path(host).resolve().parent / PARENT_DIR / RESULT_NAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "frozen phase-0a gate result not found: set PHASE0A_RESULT or RESOLVE_HOST_REPORT"
    )
