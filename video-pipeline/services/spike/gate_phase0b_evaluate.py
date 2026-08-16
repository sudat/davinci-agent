"""Phase-0B gate evaluator: recompute all five criteria from raw evidence.

Loads the frozen policy bindings (fixture manifests, Golden index, parent 0A
result), then re-derives every per-variant artifact chain and sync row through
:mod:`services.spike.gate_phase0b_checks`. Stale bindings and unclassified
readback mismatches trigger the shared cross-gate Stop marker. Writes
``gate-result.json`` and ``sync-measurements.json``; nothing authored in the
evidence tree is trusted as a pass flag.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from pydantic import BaseModel, ValidationError

from services.conform.map_models import ConformMap
from services.contracts.serialization import canonical_json_bytes
from services.fixtures.manifest_phase0b import Phase0BFixtureManifest
from services.foundation_io import atomic_write, sha256_file
from services.gates import CriterionResult, EvidenceBundleRef, GateResult
from services.gates.phase0b import PHASE_0B_CRITERIA, PHASE_0B_VARIANTS
from services.gates.serialization import canonical_gate_bytes
from services.ingest.models import SourceManifest
from services.normalize.models import NormalizeRecord
from services.resolve_bridge.build_report_models import VerifyMismatch
from services.spike.gate_phase0b_checks import (
    CheckState,
    check_drop_coverage,
    check_golden,
)
from services.spike.gate_phase0b_models import (
    CRITERION_GOLDEN,
    GOLDEN_DIR,
    RESULT_NAME,
    SYNC_NAME,
    LiveReadbackReport,
    SyncMeasurements,
    SyncRow,
    conform_map_path,
    fixture_manifest_path,
    ingest_manifest_path,
    normalize_record_path,
    readback_report_path,
)
from services.spike.gate_phase0b_verify import check_readback, collect_sync

if TYPE_CHECKING:
    from services.gates import GatePolicy

RESULT_SCHEMA: Final = "gate-result-v1"


class GoldenLoadError(Exception):
    """The frozen Golden table is missing, noncanonical, or unbound."""


@dataclass(frozen=True, slots=True)
class Evaluate0bInputs:
    policy: GatePolicy
    policy_sha256: str
    evidence: Path
    host_report_path: Path
    parent_result_path: Path


@dataclass(frozen=True, slots=True)
class Evaluate0bOutcome:
    result: GateResult
    stop_triggered: bool
    stop_criterion: str = ""
    stop_reason: str = ""
    mismatches: tuple[VerifyMismatch, ...] = ()
    variants_passed: tuple[str, ...] = ()


def load_golden(policy_golden_sha256: str) -> dict[str, object]:
    index_raw = (GOLDEN_DIR / "index.json").read_bytes()
    index = json.loads(index_raw)
    expected_path = GOLDEN_DIR / "expected.json"
    if sha256_file(expected_path) != index.get("expected_sha256"):
        raise GoldenLoadError("golden expected.json does not match the index binding")
    if hashlib.sha256(index_raw).hexdigest() != policy_golden_sha256:
        raise GoldenLoadError("golden index hash does not match the policy binding")
    payload = json.loads(expected_path.read_bytes())
    variants = payload.get("variants") if isinstance(payload, dict) else None
    if not isinstance(variants, dict):
        raise GoldenLoadError("golden expected table is malformed")
    return variants


def _load[Model: BaseModel](model: type[Model], path: Path) -> tuple[Model | None, str | None]:
    try:
        raw = path.read_bytes()
        return model.model_validate_json(raw), hashlib.sha256(raw).hexdigest()
    except (OSError, ValidationError):
        return None, None


def _host_binding(path: Path) -> tuple[str | None, str | None, str | None]:
    try:
        payload = json.loads(path.read_bytes())
        app = payload.get("application")
        if not isinstance(app, dict):
            return sha256_file(path), None, None
        return sha256_file(path), str(app.get("version")), str(app.get("build"))
    except (OSError, ValidationError, ValueError):
        return None, None, None


def _check_parent(inputs: Evaluate0bInputs, state: CheckState) -> None:
    raw = inputs.parent_result_path.read_bytes()
    parent = GateResult.model_validate_json(raw)
    parent_sha = hashlib.sha256(raw).hexdigest()
    state.note(CRITERION_GOLDEN, parent_sha)
    if (
        parent_sha not in tuple(inputs.policy.parent_gate_result_hashes)
        or parent.gate_id != "phase-0a"
        or not parent.passed
    ):
        state.fail(
            CRITERION_GOLDEN,
            "parent-gate-unbound",
            "parent phase-0a gate result does not satisfy the policy binding",
        )


def _evidence_bundle(evidence: Path) -> str:
    inventory: dict[str, str] = {}
    if evidence.is_dir():
        for path in sorted(evidence.rglob("*")):
            if path.is_file() and path.name not in (RESULT_NAME, SYNC_NAME):
                inventory[path.relative_to(evidence).as_posix()] = sha256_file(path)
    payload = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def evaluate0b(inputs: Evaluate0bInputs) -> Evaluate0bOutcome:
    state = CheckState(PHASE_0B_CRITERIA)
    golden = load_golden(inputs.policy.golden_sha256 or "")
    _check_parent(inputs, state)
    host_sha, host_version, host_build = _host_binding(inputs.host_report_path)

    all_sync_rows: list[SyncRow] = []
    stop_criterion, stop_reason = "", ""
    variants_passed: list[str] = []
    for variant in PHASE_0B_VARIANTS:
        rows_before = len(state.rows)
        fixture_raw = fixture_manifest_path(variant).read_bytes()
        fixture = Phase0BFixtureManifest.model_validate_json(fixture_raw)
        state.note(CRITERION_GOLDEN, hashlib.sha256(fixture_raw).hexdigest())
        manifest, manifest_sha = _load(
            SourceManifest, ingest_manifest_path(inputs.evidence, variant)
        )
        record, record_sha = _load(
            NormalizeRecord, normalize_record_path(inputs.evidence, variant)
        )
        conform_map, map_sha = _load(ConformMap, conform_map_path(inputs.evidence, variant))
        report, report_sha = _load(
            LiveReadbackReport, readback_report_path(inputs.evidence, variant)
        )
        for sha in (manifest_sha, record_sha, map_sha, report_sha):
            if sha is not None:
                for criterion in PHASE_0B_CRITERIA:
                    state.note(criterion, sha)
        check_golden(variant, fixture, golden, manifest, record, conform_map, state)
        if conform_map is not None:
            check_drop_coverage(variant, conform_map, state)
            all_sync_rows.extend(collect_sync(variant, fixture, conform_map, state))
        if record is not None:
            stale_criterion, stale_reason = check_readback(
                variant,
                fixture,
                record,
                report,
                host_sha,
                host_version,
                host_build,
                manifest,
                state,
            )
            if stale_criterion and not stop_criterion:
                stop_criterion, stop_reason = stale_criterion, stale_reason
        if len(state.rows) == rows_before:
            variants_passed.append(variant)

    sync = SyncMeasurements(
        schema_version="phase-0b-sync-measurements-v1",
        rows=tuple(all_sync_rows),
        all_passed=bool(all_sync_rows) and all(row.passed for row in all_sync_rows),
    )
    atomic_write(inputs.evidence / SYNC_NAME, canonical_json_bytes(sync))
    state.note(CRITERION_GOLDEN, sha256_file(inputs.evidence / SYNC_NAME))

    for criterion in PHASE_0B_CRITERIA:
        if not state.evidence_sha[criterion]:
            state.fail(criterion, "evidence-incomplete", criterion)
    passed = all(state.criteria.values()) and not stop_criterion
    bundle = _evidence_bundle(inputs.evidence)
    result = GateResult(
        schema_version=RESULT_SCHEMA,
        record_type="gate_result",
        gate_id=inputs.policy.gate_id,
        gate_version=inputs.policy.gate_version,
        policy_sha256=inputs.policy_sha256,
        passed=passed,
        evidence_bundles=cast(
            "tuple[EvidenceBundleRef, ...]",
            ({"bundle_sha256": bundle, "raw_evidence_sha256s": ["0" * 64]},),
        ),
        criteria_results=cast(
            "tuple[CriterionResult, ...]",
            tuple(
                {
                    "criterion_id": criterion,
                    "passed": state.criteria[criterion],
                    "raw_evidence_sha256s": state.evidence_sha[criterion] or ["0" * 64],
                }
                for criterion in PHASE_0B_CRITERIA
            ),
        ),
    )
    return Evaluate0bOutcome(
        result=result,
        stop_triggered=bool(stop_criterion),
        stop_criterion=stop_criterion,
        stop_reason=stop_reason,
        mismatches=tuple(
            VerifyMismatch(code=code, detail=detail) for code, detail in state.rows
        ),
        variants_passed=tuple(variants_passed),
    )


def write_result(outcome: Evaluate0bOutcome, evidence: Path) -> Path:
    target = evidence / RESULT_NAME
    atomic_write(target, canonical_gate_bytes(outcome.result))
    return target
