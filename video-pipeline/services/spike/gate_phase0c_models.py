"""Names, paths, criteria mapping, and evidence models for the Phase-0C gate.

All 0C evidence lives under ``<evidence>/runs/<fixture>/{store,preview-0,
preview-1}`` plus ``translator-record.json``; the Todo-27 ``preview.mp4`` and
``preview-trace.json`` already at the evidence root are preserved untouched.
The evaluator recomputes every assertion from these raw artifacts and never
trusts an authored pass field.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from services.contracts.primitives import Sha256, StrictModel
from services.gates.phase0c import PHASE_0C_FIXTURES

GATE0C_ID: Final = "phase-0c"
GATE0C_MARKER: Final = "phase-0c:"
GATE0C_MODULE: Final = "services.spike.run_gate"

RUNS_DIR: Final = "runs"
STORE_DIR: Final = "store"
PREVIEW0_DIR: Final = "preview-0"
PREVIEW1_DIR: Final = "preview-1"
MEDIA_DIR: Final = "media"
TRANSLATOR_RECORD_NAME: Final = "translator-record.json"
REBUILD_NAME: Final = "rebuild.json"
EVENTS_LOG_NAME: Final = "events.jsonl"
RESULT_NAME: Final = "gate-result.json"
MATRIX0C_NAME: Final = "capability-matrix-0c.json"

DEFAULT_LOCK: Final = Path("config/toolchains/phase-0c-v2.json")
MANIFEST_DIR: Final = Path("tests/fixtures/manifests/phase-0c")
GOLDEN_DIR: Final = Path("tests/goldens/reference/phase-0c")
GOLDEN_INDEX_NAME: Final = "index.json"
GOLDEN_EXPECTED_NAME: Final = "expected.json"
PARENT_DIR: Final = "phase-0b"

CLEAR_CASES: Final = ("p0c-remove-clear", "p0c-span-clear", "p0c-subtitle-clear")
AMBIGUOUS_CASES: Final = ("p0c-ambiguous-two-targets",)
CONFLICT_CASES: Final = ("p0c-locked-conflict",)

CRITERION_CLEAR: Final = "phase-0c-declared-unambiguous-commands-100-percent"
CRITERION_AMBIGUOUS: Final = "phase-0c-ambiguous-auto-apply-zero"
CRITERION_CONFLICT: Final = "phase-0c-conflict-detection-exact"
CRITERION_REBUILD: Final = "phase-0c-deterministic-plan-ir-regeneration"

STOP_STALE_BINDING: Final = "phase-0c-stop-stale-plan-binding"
STOP_UNCLASSIFIED: Final = "phase-0c-stop-unclassified-evidence-mismatch"

# Structured mismatch codes the evaluator can emit; anything that fails to
# classify into one of these (or into a criterion) is a Stop, never a pass.
CODE_GOLDEN_PLAN: Final = "golden-plan-mismatch"
CODE_GOLDEN_RECORD: Final = "golden-record-table-mismatch"
CODE_WRONG_TARGET: Final = "wrong-target-candidates"
CODE_CLEAR_NOT_APPLIED: Final = "declared-clear-not-applied"
CODE_AMBIGUOUS_AUTO_APPLIED: Final = "ambiguous-auto-applied"
CODE_MODEL_AUTHORED_DECISION: Final = "model-authored-decision"
CODE_DEFER_MUTATED_PLAN: Final = "defer-mutated-plan"
CODE_TRACE_BINDING: Final = "preview-trace-binding-mismatch"
CODE_PREVIEW_MISSING: Final = "preview-evidence-missing"
CODE_TRANSLATOR_STATUS: Final = "translator-status-not-proposal"
CODE_NEEDS_HUMAN_MISSING: Final = "needs-human-not-recorded"
CODE_CONFLICT_SHAPE: Final = "conflict-shape-mismatch"

FAULT_KINDS: Final = {
    "wrong_decision": CODE_GOLDEN_PLAN,
    "auto_applied_ambiguous": CODE_AMBIGUOUS_AUTO_APPLIED,
    "changed_unrelated_item": CODE_GOLDEN_PLAN,
    "stale_event": STOP_STALE_BINDING,
    "resolve_requirement": "resolve-bridge-refused",
}


def fixture_manifest_path(fixture_id: str) -> Path:
    return MANIFEST_DIR / f"{fixture_id}.json"


def run_dir(evidence: Path, fixture_id: str) -> Path:
    return evidence / RUNS_DIR / fixture_id


def store_dir(evidence: Path, fixture_id: str) -> Path:
    return run_dir(evidence, fixture_id) / STORE_DIR


def events_log_path(evidence: Path, fixture_id: str) -> Path:
    return store_dir(evidence, fixture_id) / EVENTS_LOG_NAME


def preview_dir(evidence: Path, fixture_id: str, generation: Literal[0, 1]) -> Path:
    name = PREVIEW0_DIR if generation == 0 else PREVIEW1_DIR
    return run_dir(evidence, fixture_id) / name


def translator_record_path(evidence: Path, fixture_id: str) -> Path:
    return run_dir(evidence, fixture_id) / TRANSLATOR_RECORD_NAME


def rebuild_path(evidence: Path, fixture_id: str) -> Path:
    return run_dir(evidence, fixture_id) / REBUILD_NAME


class GoldenIndex0C(StrictModel):
    schema_version: Literal["golden-index-v1"]
    expected_sha256: Sha256
    fixture_ids: tuple[str, ...] = Field(min_length=1)
    fixture_manifest_sha256s: dict[str, Sha256]


class Mismatch0C(StrictModel):
    """A structured criterion mismatch; the 0C path stays Resolve-free."""

    code: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class RebuildReport0C(StrictModel):
    """Evaluator-written record of the deterministic rebuild recomputation."""

    schema_version: Literal["phase-0c-rebuild-v1"]
    fixture_id: str = Field(min_length=1)
    versions_applied: tuple[str, ...]
    deterministic_hash: Sha256
    base_plan_sha256: Sha256
    head_plan_sha256: Sha256
    head_ir_sha256: Sha256
    deferred: bool
    plan_bytes_unchanged: bool


__all__ = [
    "AMBIGUOUS_CASES",
    "CLEAR_CASES",
    "CODE_AMBIGUOUS_AUTO_APPLIED",
    "CODE_CLEAR_NOT_APPLIED",
    "CODE_CONFLICT_SHAPE",
    "CODE_DEFER_MUTATED_PLAN",
    "CODE_GOLDEN_PLAN",
    "CODE_GOLDEN_RECORD",
    "CODE_MODEL_AUTHORED_DECISION",
    "CODE_NEEDS_HUMAN_MISSING",
    "CODE_PREVIEW_MISSING",
    "CODE_TRACE_BINDING",
    "CODE_TRANSLATOR_STATUS",
    "CODE_WRONG_TARGET",
    "CONFLICT_CASES",
    "CRITERION_AMBIGUOUS",
    "CRITERION_CLEAR",
    "CRITERION_CONFLICT",
    "CRITERION_REBUILD",
    "EVENTS_LOG_NAME",
    "FAULT_KINDS",
    "GATE0C_ID",
    "GATE0C_MARKER",
    "GOLDEN_DIR",
    "GOLDEN_EXPECTED_NAME",
    "GOLDEN_INDEX_NAME",
    "MATRIX0C_NAME",
    "PARENT_DIR",
    "PHASE_0C_CASES",
    "PREVIEW0_DIR",
    "PREVIEW1_DIR",
    "REBUILD_NAME",
    "RESULT_NAME",
    "RUNS_DIR",
    "STOP_STALE_BINDING",
    "STOP_UNCLASSIFIED",
    "STORE_DIR",
    "TRANSLATOR_RECORD_NAME",
    "GoldenIndex0C",
    "Mismatch0C",
    "RebuildReport0C",
    "events_log_path",
    "fixture_manifest_path",
    "preview_dir",
    "rebuild_path",
    "run_dir",
    "store_dir",
    "translator_record_path",
]

PHASE_0C_CASES: Final = PHASE_0C_FIXTURES
