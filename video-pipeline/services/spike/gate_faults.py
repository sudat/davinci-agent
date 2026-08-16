"""Offline fault scenarios for the Phase-0A gate (``QA_FAULT_FIXTURE`` mode).

Each fault synthesizes a complete fake evidence tree with one injected
defect, runs the real gate evaluator over it, and prints the observation.
Exit code is ``EXIT_FAULT`` whether or not the injected fault was detected;
the printed observation discriminates. Stop faults additionally write the
stop marker and demonstrate that a rerun is refused while it is present.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.contracts.primitives import StrictModel
from services.foundation_io import sha256_file
from services.gates import GatePolicy
from services.resolve_bridge.build_report_fakes import FakeBuildTools
from services.spike.gate_evaluate import GateEvaluationInputs, evaluate
from services.spike.gate_fakes import HOST_NAME, FaultKnobs, synthesize
from services.spike.gate_models import EXIT_FAULT, MARKER
from services.spike.stop_rules import (
    STOP_CAPABILITY,
    STOP_IDENTITY,
    load_stop,
    record_stop,
    stop_marker_path,
    stop_recorded,
)

if TYPE_CHECKING:
    from services.resolve_bridge.build_report_models import VerifyMismatch

FAULT_KINDS: Final = {
    "stale_prior_build_evidence": "stale-prior-build-evidence",
    "restart_drift": "restart-binding-drift",
    "partial_timeline_dependence": "partial-timeline-dependence",
    "stop_source_identity": STOP_IDENTITY,
    "stop_capability_missing": STOP_CAPABILITY,
}


class GateFaultSpec(StrictModel):
    fault: str


class GateFaultSpecError(Exception):
    """The fault fixture is invalid."""


def _knobs(fault: str) -> FaultKnobs:
    return FaultKnobs(
        stale_prior_build=fault == "stale_prior_build_evidence",
        restart_drift=fault == "restart_drift",
        partial_dependence=fault == "partial_timeline_dependence",
        stop_source_identity=fault == "stop_source_identity",
        stop_capability_missing=fault == "stop_capability_missing",
    )


def _print_rows(rows: tuple[VerifyMismatch, ...]) -> None:
    for row in rows:
        print(f"{MARKER} mismatch code={row.code} detail={row.detail}", file=sys.stderr)


def run_fault_cli(spec_path: Path, policy_path: Path, manifest_path: Path) -> int:
    try:
        spec = GateFaultSpec.model_validate_json(spec_path.read_bytes())
        policy = GatePolicy.model_validate_json(policy_path.read_bytes())
    except (OSError, ValueError) as error:
        print(f"{MARKER} fault-fixture invalid: {error}", file=sys.stderr)
        return EXIT_FAULT
    if spec.fault not in FAULT_KINDS:
        print(f"{MARKER} fault-fixture invalid: unknown fault {spec.fault!r}", file=sys.stderr)
        return EXIT_FAULT
    if policy.fixture_manifest_sha256 != sha256_file(manifest_path):
        print(
            f"{MARKER} fault-fixture invalid: manifest does not match the policy",
            file=sys.stderr,
        )
        return EXIT_FAULT

    with tempfile.TemporaryDirectory(prefix="gate-fault-") as scratch_name:
        evidence = Path(scratch_name) / "phase-0a"
        fixture = synthesize(manifest_path, evidence, _knobs(spec.fault))
        outcome = evaluate(
            GateEvaluationInputs(
                policy=policy,
                policy_sha256=sha256_file(policy_path),
                evidence=evidence,
                manifest_path=manifest_path,
                host_report_path=evidence / HOST_NAME,
                fixture_dir=fixture,
                ffmpeg_bin=evidence / "ffmpeg",
                ffprobe_bin=evidence / "ffprobe",
                port=FakeBuildTools(),
            )
        )
        _print_rows(outcome.mismatches)
        expected_code = FAULT_KINDS[spec.fault]
        if outcome.stop_triggered:
            print(f"{MARKER} STOP code={outcome.stop_criterion} reason={outcome.stop_reason}")
            marker = record_stop(evidence, outcome.stop_criterion, outcome.stop_reason)
            refused = stop_recorded(evidence)
            loaded = load_stop(stop_marker_path(evidence))
            print(f"{MARKER} stop-marker-written path={marker.name}")
            print(
                f"{MARKER} rerun-refused={str(refused is not None).lower()} "
                f"criterion={loaded.criterion_id}"
            )
            if outcome.stop_criterion != expected_code:
                print(
                    f"ERROR: expected stop {expected_code} but got {outcome.stop_criterion}",
                    file=sys.stderr,
                )
                return EXIT_FAULT
            observed = True
        else:
            observed = expected_code in {row.code for row in outcome.mismatches}
            print(f"{MARKER} fault-observed={str(observed).lower()} code={expected_code}")
        if not observed:
            print(f"ERROR: fault {spec.fault!r} not observed", file=sys.stderr)
    return EXIT_FAULT
