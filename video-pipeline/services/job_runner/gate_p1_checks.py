"""Criterion recomputation (part 1) for the Phase-1 Technical Gate (Todo 46).

Recomputed from raw evidence only — run reports, bundles, review-store
plan/IR bytes, observations, frozen manifests, goldens. Criterion mapping:

- ``phase-1-fixtures-100-golden-match`` (this module): policy/manifest/golden/
  parent/lock bindings, approval-ingress refusals (an auto-created operator
  record fails the gate), all five E2E chains complete with hash-verified
  bundles, candidate/selection table parity vs the goldens.
- ``phase-1-must-include-zero-miss`` (this module): recomputed from the
  committed review plan against the declared rules, golden cross-checked.

Placement/ordering, review coverage, and determinism live in
``gate_p1_flow_checks``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from services.foundation_io import sha256_file
from services.gates import GatePolicy, GateResult
from services.gates.phase1_technical import PHASE_1_TECHNICAL_CRITERIA, PHASE_1_TECHNICAL_FIXTURES
from services.job_runner.gate_p1_parity import load_ir

if TYPE_CHECKING:
    from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
    from services.job_runner.gate_p1_models import FixtureObservation

C_E2E = PHASE_1_TECHNICAL_CRITERIA[0]
C_MUST = PHASE_1_TECHNICAL_CRITERIA[1]
CHAIN_RUNS = 2


@dataclass(frozen=True, slots=True)
class Mismatch:
    code: str
    detail: str


@dataclass(slots=True)
class CheckState:
    criteria: dict[str, bool] = field(
        default_factory=lambda: dict.fromkeys(PHASE_1_TECHNICAL_CRITERIA, True)
    )
    evidence: dict[str, list[str]] = field(
        default_factory=lambda: {key: [] for key in PHASE_1_TECHNICAL_CRITERIA}
    )
    mismatches: list[Mismatch] = field(default_factory=list)

    def fail(self, criterion: str, code: str, detail: str) -> None:
        self.criteria[criterion] = False
        self.mismatches.append(Mismatch(code=code, detail=detail))

    def note(self, criterion: str, *shas: str) -> None:
        for sha in shas:
            if sha and sha not in self.evidence[criterion]:
                self.evidence[criterion].append(sha)


def op_sha(model: BaseModel) -> str:
    from services.contracts.serialization import canonical_json_bytes  # noqa: PLC0415

    return hashlib.sha256(canonical_json_bytes(model)).hexdigest()


def mapping_of(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise MalformedEvidenceError(f"expected an object, got {value!r}")
    return value


def str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise MalformedEvidenceError(f"expected an array, got {value!r}")
    return [str(item) for item in value]


class MalformedEvidenceError(Exception):
    """A raw evidence artifact did not parse into its expected shape."""


def check_bindings(  # noqa: PLR0913, PLR0917 (bindings verify every frozen reference)
    policy: GatePolicy,
    manifest_dir: Path,
    goldens_dir: Path,
    toolchain_lock: Path,
    parents: Mapping[str, Path],
    state: CheckState,
) -> None:
    combined = hashlib.sha256()
    try:
        for fixture_id in PHASE_1_TECHNICAL_FIXTURES:
            combined.update((manifest_dir / f"{fixture_id}.json").read_bytes())
    except OSError as error:
        state.fail(C_E2E, "evidence-incomplete", f"manifest unreadable: {error}")
        return
    if policy.fixture_manifest_sha256 != combined.hexdigest():
        state.fail(C_E2E, "fixture-manifest-drift", "manifest bytes drift from the policy binding")
    index_raw = (goldens_dir / "index.json").read_bytes()
    if policy.golden_sha256 != hashlib.sha256(index_raw).hexdigest():
        state.fail(C_E2E, "golden-index-drift", "golden index does not match the policy binding")
    index = mapping_of(json.loads(index_raw))
    expected = (goldens_dir / "expected.json").read_bytes()
    if index["expected_sha256"] != hashlib.sha256(expected).hexdigest():
        state.fail(C_E2E, "golden-expected-drift", "golden tables do not match the index binding")
    if policy.toolchain_lock_sha256 is None or policy.toolchain_lock_sha256 != sha256_file(
        toolchain_lock
    ):
        state.fail(C_E2E, "toolchain-lock-drift", "toolchain lock bytes drift")
    if set(parents) != {"phase-0c", "control-plane-baseline"} or len(parents) != len(
        policy.parent_gate_result_hashes
    ):
        state.fail(C_E2E, "parent-gate-unbound", "parent gate set does not match the policy")
        return
    for path in parents.values():
        raw = path.read_bytes()
        result = GateResult.model_validate_json(raw)
        if (
            hashlib.sha256(raw).hexdigest() not in policy.parent_gate_result_hashes
            or not result.passed
        ):
            state.fail(C_E2E, "parent-gate-drift", f"parent gate result drift: {path}")


def check_approvals(observation_path: Path, state: CheckState) -> None:
    from services.job_runner.gate_cp_models import GateObservation  # noqa: PLC0415

    try:
        observation = GateObservation.model_validate_json(observation_path.read_bytes())
    except (OSError, ValueError) as error:
        state.fail(C_E2E, "approvals-observation-missing", str(error))
        return
    state.note(C_E2E, sha256_file(observation_path))
    probes = {op.name: op.result for op in observation.operations}
    for name, expected in (
        ("automation-ingress-refused", "automation-refused"),
        ("non-tty-ingress-refused", "not-a-tty"),
    ):
        actual = probes.get(name)
        if actual == "unexpected-success":
            state.fail(C_E2E, "operator-record-auto-created", f"{name} minted a real record")
        elif actual != expected:
            state.fail(C_E2E, "approvals-probe-missing", f"{name} -> {actual!r}")
    if observation.fields.get("fixture_record_fixture_only") != "true":
        state.fail(C_E2E, "fixture-label-missing", "gate-created records must stay fixture-marked")
    if probes.get("operator-gate-fixture-refused") == "authorized":
        state.fail(
            C_E2E, "operator-record-auto-created", "a fixture record authorized the operator gate"
        )


def load_reports(
    observation: FixtureObservation,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    run1 = mapping_of(json.loads(Path(observation.fields["run1_report"]).read_bytes()))
    run2 = mapping_of(json.loads(Path(observation.fields["run2_report"]).read_bytes()))
    return run1, run2


def initial_ir_path(observation: FixtureObservation) -> Path:
    return Path(observation.run1_dir) / "review-store" / "ir-v1.json"


def check_e2e(
    observation: FixtureObservation,
    golden: Mapping[str, object],
    manifest: Phase1TechnicalFixtureManifest,
    state: CheckState,
) -> None:
    fixture = observation.fixture_id
    for op in observation.operations:
        state.note(C_E2E, op_sha(op))
    chain = [op for op in observation.operations if op.name == "chain-run"]
    if len(chain) != CHAIN_RUNS or any(
        op.result != "ok" or not op.detail.endswith(":PREVIEW_READY") for op in chain
    ):
        state.fail(
            C_E2E, "evidence-incomplete", f"{fixture}: chain did not reach PREVIEW_READY twice"
        )
        return
    state.note(
        C_E2E,
        sha256_file(Path(observation.fields["run1_report"])),
        sha256_file(Path(observation.fields["run2_report"])),
    )
    ir1 = load_ir(initial_ir_path(observation))
    selected = [row.item_id for row in ir1["video"]]
    golden_selection = mapping_of(golden["selection"])
    if selected != str_list(golden_selection["selected_ids"]):
        state.fail(C_E2E, "selection-mismatch", f"{fixture}: selected {selected} != golden")
    segments = {
        segment.segment_id: segment for segment in manifest.transcript.segments
    }
    dropped = sorted(set(segments) - set(selected))
    if dropped != sorted(str_list(golden_selection["dropped_ids"])):
        state.fail(C_E2E, "dropped-mismatch", f"{fixture}: dropped {dropped} != golden")
    total = sum(row.source_end - row.source_start for row in ir1["video"])
    if total != golden_selection["total_selected_frames"]:
        state.fail(C_E2E, "total-frames-mismatch", f"{fixture}: total frames {total} != golden")
    from services.job_runner.gate_p1_candidates import (  # noqa: PLC0415 (cycle break)
        check_candidate_table,
    )

    check_candidate_table(segments, manifest, golden, selected, state, fixture)


def check_must_include(
    observation: FixtureObservation,
    golden: Mapping[str, object],
    manifest: Phase1TechnicalFixtureManifest,
    state: CheckState,
) -> None:
    must = list(manifest.editorial_rules.must_include.segment_ids)
    selected = {row.item_id for row in load_ir(initial_ir_path(observation))["video"]}
    missed = sorted(set(must) - selected)
    golden_missed = str_list(mapping_of(golden["selection"])["must_include_missed"])
    if missed or golden_missed:
        state.fail(
            C_MUST,
            "must-include-miss",
            f"{observation.fixture_id}: recomputed misses {missed}, golden {golden_missed}",
        )
    state.note(C_MUST, sha256_file(initial_ir_path(observation)))


__all__ = [
    "CHAIN_RUNS",
    "C_E2E",
    "C_MUST",
    "CheckState",
    "MalformedEvidenceError",
    "Mismatch",
    "check_approvals",
    "check_bindings",
    "check_e2e",
    "check_must_include",
    "initial_ir_path",
    "load_reports",
    "mapping_of",
    "op_sha",
    "str_list",
]
