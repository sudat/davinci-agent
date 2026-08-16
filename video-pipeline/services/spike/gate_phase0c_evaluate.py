"""Phase-0C gate evaluator: recompute all four criteria from raw evidence.

Loads the frozen Golden tables through the policy-bound index, validates the
parent Phase-0B result binding, then recomputes every per-case assertion from
the raw store/translator/preview artifacts. Unclassifiable evidence and
malformed artifacts are a Stop (fail-closed, never proceed); the reducer's
stale-version family is the stale-binding Stop. Writes ``rebuild.json`` per
case and ``gate-result.json``; authored pass fields are never trusted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates import CriterionResult, EvidenceBundleRef, GateResult
from services.gates.phase0c import PHASE_0C_CRITERIA
from services.gates.serialization import canonical_gate_bytes
from services.spike.gate_phase0c_checks import CheckState0c, GoldenCase0C
from services.spike.gate_phase0c_evidence import (
    CaseEvidence,
    EvidenceLoadError,
    load_case_evidence,
)
from services.spike.gate_phase0c_golden import GoldenLoadError, load_golden0c
from services.spike.gate_phase0c_models import (
    PHASE_0C_CASES,
    REBUILD_NAME,
    RESULT_NAME,
    STOP_UNCLASSIFIED,
    Mismatch0C,
    RebuildReport0C,
    rebuild_path,
)
from services.spike.gate_phase0c_verify import check_case

if TYPE_CHECKING:
    from services.gates import GatePolicy

RESULT_SCHEMA: Final = "gate-result-v1"
_PARENT_CRITERION: Final = PHASE_0C_CRITERIA[0]


@dataclass(frozen=True, slots=True)
class Evaluate0cInputs:
    policy: GatePolicy
    policy_sha256: str
    evidence: Path
    parent_result_path: Path


@dataclass(frozen=True, slots=True)
class Evaluate0cOutcome:
    result: GateResult
    stop_triggered: bool
    stop_criterion: str = ""
    stop_reason: str = ""
    mismatches: tuple[Mismatch0C, ...] = ()
    cases_passed: tuple[str, ...] = ()


def _check_parent(inputs: Evaluate0cInputs, state: CheckState0c) -> None:
    raw = inputs.parent_result_path.read_bytes()
    parent = GateResult.model_validate_json(raw)
    parent_sha = hashlib.sha256(raw).hexdigest()
    for criterion in PHASE_0C_CRITERIA:
        state.note(criterion, parent_sha)
    if (
        parent_sha not in tuple(inputs.policy.parent_gate_result_hashes)
        or parent.gate_id != "phase-0b"
        or not parent.passed
    ):
        state.fail(
            _PARENT_CRITERION,
            "parent-gate-unbound",
            "parent phase-0b gate result does not satisfy the policy binding",
        )


def _evidence_bundle(evidence: Path) -> str:
    inventory: dict[str, str] = {}
    if evidence.is_dir():
        for path in sorted(evidence.rglob("*")):
            if path.is_file() and path.name not in (RESULT_NAME, REBUILD_NAME):
                inventory[path.relative_to(evidence).as_posix()] = sha256_file(path)
    payload = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _write_rebuild(evidence: Path, bundle: CaseEvidence, deterministic_hash: str) -> str:
    """Record the recomputed rebuild identity; skipped when replay stopped."""

    if not deterministic_hash:
        return ""
    report = RebuildReport0C(
        schema_version="phase-0c-rebuild-v1",
        fixture_id=bundle.fixture_id,
        versions_applied=tuple(
            event.result_plan_version or ""
            for event in bundle.events
            if event.kind == "decision_applied"
        ),
        deterministic_hash=deterministic_hash,
        base_plan_sha256=sha256_file(
            bundle.preview1_dir.parent / "store" / "plan-v1.json"
        ),
        head_plan_sha256=hashlib.sha256(canonical_model_bytes(bundle.plan_head)).hexdigest(),
        head_ir_sha256=bundle.ir_head_sha256,
        deferred=bundle.head_version == 1,
        plan_bytes_unchanged=bundle.head_version == 1,
    )
    target = rebuild_path(evidence, bundle.fixture_id)
    atomic_write(target, canonical_model_bytes(report))
    return sha256_file(target)


def _evaluate_case(
    inputs: Evaluate0cInputs,
    fixture_id: str,
    golden: dict[str, GoldenCase0C],
    state: CheckState0c,
) -> tuple[str, str, bool]:
    """Returns (stop_criterion, stop_reason, case_passed)."""

    try:
        bundle = load_case_evidence(inputs.evidence, fixture_id)
    except EvidenceLoadError as error:
        for criterion in PHASE_0C_CRITERIA:
            state.fail(criterion, "evidence-incomplete", str(error))
        return (STOP_UNCLASSIFIED, str(error), False)
    rows_before = len(state.rows)
    case_stop_criterion, case_stop_reason, deterministic_hash = check_case(
        bundle, golden[fixture_id], state
    )
    rebuild_sha = _write_rebuild(inputs.evidence, bundle, deterministic_hash)
    if rebuild_sha:
        for criterion in PHASE_0C_CRITERIA:
            state.note(criterion, rebuild_sha)
    if case_stop_criterion:
        return (case_stop_criterion, case_stop_reason, False)
    return ("", "", len(state.rows) == rows_before)


def evaluate0c(inputs: Evaluate0cInputs) -> Evaluate0cOutcome:
    state = CheckState0c()
    golden = load_golden0c(inputs.policy)
    _check_parent(inputs, state)
    stop_criterion, stop_reason = "", ""
    cases_passed: list[str] = []
    for fixture_id in PHASE_0C_CASES:
        case_stop_criterion, case_stop_reason, case_passed = _evaluate_case(
            inputs, fixture_id, golden, state
        )
        if case_stop_criterion and not stop_criterion:
            stop_criterion, stop_reason = case_stop_criterion, case_stop_reason
        if case_passed:
            cases_passed.append(fixture_id)
    for criterion in PHASE_0C_CRITERIA:
        if not state.evidence_sha[criterion]:
            state.fail(criterion, "evidence-incomplete", criterion)
    if stop_criterion:
        for criterion in PHASE_0C_CRITERIA:
            if state.criteria[criterion]:
                state.fail(criterion, "stop-triggered", stop_reason)
    result = _build_result(inputs, state, stop_criterion)
    return Evaluate0cOutcome(
        result=result,
        stop_triggered=bool(stop_criterion),
        stop_criterion=stop_criterion,
        stop_reason=stop_reason,
        mismatches=tuple(
            Mismatch0C(code=code, detail=detail) for code, detail in state.rows
        ),
        cases_passed=tuple(cases_passed),
    )


def _build_result(inputs: Evaluate0cInputs, state: CheckState0c, stop_criterion: str) -> GateResult:
    passed = all(state.criteria.values()) and not stop_criterion
    return GateResult(
        schema_version=RESULT_SCHEMA,
        record_type="gate_result",
        gate_id=inputs.policy.gate_id,
        gate_version=inputs.policy.gate_version,
        policy_sha256=inputs.policy_sha256,
        passed=passed,
        evidence_bundles=cast(
            "tuple[EvidenceBundleRef, ...]",
            (
                {
                    "bundle_sha256": _evidence_bundle(inputs.evidence),
                    "raw_evidence_sha256s": ["0" * 64],
                },
            ),
        ),
        criteria_results=cast(
            "tuple[CriterionResult, ...]",
            tuple(
                {
                    "criterion_id": criterion,
                    "passed": state.criteria[criterion],
                    "raw_evidence_sha256s": state.evidence_sha[criterion] or ["0" * 64],
                }
                for criterion in PHASE_0C_CRITERIA
            ),
        ),
    )


def write_result(outcome: Evaluate0cOutcome, evidence: Path) -> Path:
    target = evidence / RESULT_NAME
    atomic_write(target, canonical_gate_bytes(outcome.result))
    return target


__all__ = [
    "Evaluate0cInputs",
    "Evaluate0cOutcome",
    "GoldenLoadError",
    "evaluate0c",
    "load_golden0c",
    "write_result",
]
