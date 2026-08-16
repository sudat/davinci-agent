"""Phase-0C exit-gate CLI (dispatched from ``services.spike.run_gate``).

``run_gate phase-0c --policy <frozen policy> --evidence <dir>`` verifies the
policy bindings, refuses to start while a stop marker is recorded, drives the
five-case preview/review suite on the pinned-ffmpeg path (Resolve-free; real
previews, bounded per render), recomputes all four frozen criteria from raw
evidence, appends only genuinely new capability findings, and writes
``gate-result.json``. Exit codes: 0 pass, 1 fail, 2 usage/fault, 3 stop
(recorded or triggered). Operator decisions inside the suite are
fixture-marked records; human approvals are NOT exercised by this Spike.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.gates import GatePolicy
from services.gates.serialization import canonical_gate_bytes
from services.preview.tools import PinnedTools, load_pinned_tools
from services.spike.gate_models import (
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_STOP,
)
from services.spike.gate_phase0c_driver import Gate0cDriverError, drive_gate
from services.spike.gate_phase0c_evaluate import (
    Evaluate0cInputs,
    Evaluate0cOutcome,
    GoldenLoadError,
    evaluate0c,
    write_result,
)
from services.spike.gate_phase0c_matrix import append0c_findings, derive0c_findings
from services.spike.gate_phase0c_models import (
    DEFAULT_LOCK,
    GATE0C_ID,
    GATE0C_MARKER,
    GOLDEN_DIR,
    GOLDEN_INDEX_NAME,
    PARENT_DIR,
    PHASE_0C_CASES,
    RESULT_NAME,
    fixture_manifest_path,
)
from services.spike.stop_rules import record_stop, stop_recorded

DEFAULT_BUDGET_SECONDS: Final = 1200.0


class Gate0cBindingError(Exception):
    """The policy does not bind the current frozen 0C inputs."""


def preflight0c(policy: GatePolicy, lock_path: Path) -> None:
    combined = hashlib.sha256()
    for fixture_id in PHASE_0C_CASES:
        combined.update(fixture_manifest_path(fixture_id).read_bytes())
    if combined.hexdigest() != policy.fixture_manifest_sha256:
        raise Gate0cBindingError("combined fixture-manifest hash drift vs policy")
    if _sha256(lock_path) != policy.toolchain_lock_sha256:
        raise Gate0cBindingError("toolchain lock hash drift vs policy")
    if _sha256(GOLDEN_DIR / GOLDEN_INDEX_NAME) != policy.golden_sha256:
        raise Gate0cBindingError("golden index hash drift vs policy")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tools(arguments: argparse.Namespace, lock_path: Path) -> PinnedTools:
    if arguments.ffmpeg is not None and arguments.ffprobe is not None:
        tools = PinnedTools(
            ffmpeg=arguments.ffmpeg,
            ffprobe=arguments.ffprobe,
            ffmpeg_sha256=_sha256(arguments.ffmpeg),
            ffprobe_sha256=_sha256(arguments.ffprobe),
        )
        tools.verify_current()
        return tools
    return load_pinned_tools(lock_path)


def _fixture_dir(arguments: argparse.Namespace, tools: PinnedTools) -> Path:
    if arguments.fixture_dir is not None:
        return arguments.fixture_dir
    return tools.ffmpeg.parents[3] / "phase-0a" / "fixture"


def _parent_result(arguments: argparse.Namespace) -> Path:
    if arguments.parent_result is not None:
        return arguments.parent_result
    return arguments.evidence.parent / PARENT_DIR / RESULT_NAME


def run_cli(arguments: argparse.Namespace) -> int:
    evidence = arguments.evidence
    try:
        policy_raw = arguments.policy.read_bytes()
        policy = GatePolicy.model_validate_json(policy_raw)
    except (OSError, ValidationError) as error:
        print(f"{GATE0C_MARKER} FAIL code=policy-invalid {error}", file=sys.stderr)
        return EXIT_FAIL
    if policy_raw != canonical_gate_bytes(policy):
        print(f"{GATE0C_MARKER} FAIL code=policy-noncanonical", file=sys.stderr)
        return EXIT_FAIL
    if policy.gate_id != GATE0C_ID:
        print(f"{GATE0C_MARKER} FAIL code=policy-wrong-gate {policy.gate_id}", file=sys.stderr)
        return EXIT_FAIL
    policy_sha256 = hashlib.sha256(policy_raw).hexdigest()
    print(f"{GATE0C_MARKER} policy sha256={policy_sha256}")

    stopped = stop_recorded(evidence)
    if stopped is not None:
        print(
            f"{GATE0C_MARKER} STOP-REFUSED gate={stopped.gate_id} "
            f"criterion={stopped.criterion_id} reason={stopped.reason}"
        )
        return EXIT_STOP

    lock_path = arguments.lock or DEFAULT_LOCK
    try:
        preflight0c(policy, lock_path)
        tools = _tools(arguments, lock_path)
    except (Gate0cBindingError, OSError, ValidationError, ValueError) as error:
        print(f"{GATE0C_MARKER} FAIL code=policy-binding-drift {error}", file=sys.stderr)
        return EXIT_FAIL
    fixture_dir = _fixture_dir(arguments, tools)
    parent_result = _parent_result(arguments)
    print(f"{GATE0C_MARKER} pinned-ffmpeg {tools.ffmpeg}")
    print(f"{GATE0C_MARKER} fixture-media {fixture_dir}")
    print(
        f"{GATE0C_MARKER} boundary=operator-decisions-are-fixture-marked-records "
        "human-approvals-not-exercised"
    )

    try:
        drive_gate(evidence, tools, fixture_dir, arguments.budget)
    except (Gate0cDriverError, OSError, ValidationError, ValueError) as error:
        print(f"{GATE0C_MARKER} FAIL code=drive-error {error}", file=sys.stderr)
        return EXIT_FAIL

    try:
        outcome = evaluate0c(
            Evaluate0cInputs(
                policy=policy,
                policy_sha256=policy_sha256,
                evidence=evidence,
                parent_result_path=parent_result,
            )
        )
    except (GoldenLoadError, OSError, ValidationError) as error:
        print(f"{GATE0C_MARKER} FAIL code=golden-unbound {error}", file=sys.stderr)
        return EXIT_FAIL
    appended = append0c_findings(
        derive0c_findings(evidence), arguments.capabilities_dir, "21.0.4"
    )
    print(f"{GATE0C_MARKER} capabilities new-findings-appended={len(appended)}")
    result_path = write_result(outcome, evidence)
    return _report(outcome, result_path, evidence)


def _report(outcome: Evaluate0cOutcome, result_path: Path, evidence: Path) -> int:
    passed_count = sum(1 for row in outcome.result.criteria_results if row.passed)
    print(f"{GATE0C_MARKER} criteria {passed_count}/{len(outcome.result.criteria_results)} passed")
    for case in outcome.cases_passed:
        print(f"{GATE0C_MARKER} case {case} PASS")
    for row in outcome.mismatches:
        print(f"{GATE0C_MARKER} mismatch code={row.code} detail={row.detail}")
    if outcome.stop_triggered:
        marker = record_stop(
            evidence, outcome.stop_criterion, outcome.stop_reason, gate_id=GATE0C_ID
        )
        print(f"{GATE0C_MARKER} STOP code={outcome.stop_criterion} reason={outcome.stop_reason}")
        print(f"{GATE0C_MARKER} stop-marker={marker}")
        return EXIT_STOP
    if not outcome.result.passed:
        print(f"{GATE0C_MARKER} FAIL gate-result={result_path}")
        return EXIT_FAIL
    print(f"{GATE0C_MARKER} PASS gate-result={result_path}")
    return EXIT_PASS


__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "Gate0cBindingError",
    "preflight0c",
    "run_cli",
]
