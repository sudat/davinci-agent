"""Value models, artifact names, and exit codes for the Phase-0A gate runner.

All gate evidence artifacts live under the gate evidence directory using the
relative names defined here; every hash is recomputed from those files at
evaluation time, never read from a companion index.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from services.contracts.primitives import Identifier, Sha256, StrictModel

GATE_ID: Final = "phase-0a"
GATE_VERSION: Final = "v1"
MARKER: Final = "phase-0a:"
GATE_MODULE: Final = "services.spike.run_gate"

EXIT_PASS: Final = 0
EXIT_FAIL: Final = 1
EXIT_FAULT: Final = 2
EXIT_STOP: Final = 3
EXIT_UNAVAILABLE: Final = 4

RUN_COUNT: Final = 6
RUNS_DIR: Final = "runs"
RESTART_DIR: Final = "restart"
RECOVERY_DIR: Final = "recovery"
PROBES_DIR: Final = "probes"
REPORT_NAME: Final = "build-report.json"
RESTART_NAME: Final = "restart-evidence.json"
RECOVERY_NAME: Final = "recovery-evidence.json"
PROBES_NAME: Final = "capability-probes.json"
MATRIX_NAME: Final = "capability-matrix.json"
RESULT_NAME: Final = "gate-result.json"
STOP_NAME: Final = "gate-stop-marker.json"
REBUILD_DIR: Final = "rebuild"

DEFAULT_BUDGET_SECONDS: Final = 1200.0
RESTART_TIMEOUT_SECONDS: Final = 240.0
PARTIAL_ITEMS: Final = 2


def run_dir(evidence: Path, index: int) -> Path:
    return evidence / RUNS_DIR / f"run-{index}"


def run_report_path(evidence: Path, index: int) -> Path:
    return run_dir(evidence, index) / REPORT_NAME


class EvidenceRef(StrictModel):
    path: str = Field(min_length=1)
    sha256: Sha256


class RunRecord(StrictModel):
    run_index: int = Field(ge=1)
    phase: Literal["before-restart", "after-restart"]
    report_rel: str = Field(min_length=1)
    report_sha256: Sha256
    render_rel: str = Field(min_length=1)
    render_sha256: Sha256
    fingerprint: Sha256
    frame_delta: int = Field(ge=0)


class BindingSnapshot(StrictModel):
    product_name: str = Field(min_length=1)
    version_core: str = Field(min_length=1)
    build_number: int
    version_string: str = Field(min_length=1)


class RestartRecord(StrictModel):
    before: BindingSnapshot
    after: BindingSnapshot
    before_pid: int | None
    after_pid: int | None
    quit_at: str = Field(min_length=1)
    exited_at: str = Field(min_length=1)
    relaunch_at: str = Field(min_length=1)
    connected_at: str = Field(min_length=1)
    quit_method: str = Field(min_length=1)
    error: str = ""


class PartialBuildRecord(StrictModel):
    project_name: str = Field(min_length=1)
    timeline_name: str = Field(min_length=1)
    items_placed: int = Field(gt=0)
    partial_fingerprint: Sha256


class RecoveryRecord(StrictModel):
    partial: PartialBuildRecord
    rebuild_report_rel: str = Field(min_length=1)
    rebuild_report_sha256: Sha256
    rebuild_project_name: str = Field(min_length=1)
    rebuild_fingerprint: Sha256
    rebuild_loaded_partial: bool
    partial_reread_items: int = Field(gt=0)
    partial_reread_fingerprint: Sha256
    partial_deleted: bool


class CapabilityProbes(StrictModel):
    source_end_frame_raw: str = Field(min_length=1)
    source_end_frame_type: str = Field(min_length=1)
    source_end_frame_computed: int = Field(ge=0)
    probe_item_id: str = Field(min_length=1)
    probe_record_start: int = Field(ge=0)
    probe_record_end: int = Field(ge=0)
    frame_origin: int = Field(ge=0)
    job_status_raw: str = Field(min_length=1)
    job_marks_in: int | None
    job_marks_out: int | None


class GateStopRecord(StrictModel):
    gate_id: Identifier
    criterion_id: Identifier
    reason: str = Field(min_length=1)
    recorded_at: str = Field(min_length=1)


class CapabilityEntry(StrictModel):
    capability: Identifier
    api_available: bool
    live_verified: bool
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    limitations: str = Field(min_length=1)


class ApiFindingEntry(StrictModel):
    finding: Identifier
    api_available: bool
    live_verified: bool
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    limitations: str = Field(min_length=1)


class CapabilityMatrix(StrictModel):
    schema_version: str = Field(min_length=1)
    resolve_version: str = Field(min_length=1)
    resolve_build: str = Field(min_length=1)
    capabilities: tuple[CapabilityEntry, ...] = Field(min_length=1)
    findings: tuple[ApiFindingEntry, ...] = ()
