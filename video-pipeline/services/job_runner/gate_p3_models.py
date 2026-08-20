"""Phase-3 gate evidence models (Todo 62).

Every number the evaluator recomputes arrives through these strict models:
the two snapshot builds (structure tables, presentation hashes, route,
parity/QC verdicts, render bindings), the Phase-2 regression rerun, and the
static Builder inspection records (channel branching, Phase-4 imports).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from services.contracts.primitives import StrictModel

type SnapshotId = Literal["p3-brand-a", "p3-brand-b"]
type SideName = Literal["preview", "final"]

Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", strict=True)]


class P3StructRow(StrictModel):
    """One editorial structure row: Decision id, source span, record span."""

    item_id: str
    kind: Literal["video", "audio", "subtitle"]
    track_index: int = Field(strict=True)
    source_start: int = Field(strict=True)
    source_end: int = Field(strict=True)
    record_start: int = Field(strict=True)
    record_end: int = Field(strict=True)


class P3PresentationHashes(StrictModel):
    """The presentation identity of one snapshot build."""

    manifest_sha256: Hex64
    profile_snapshot_sha256: Hex64
    asset_sha256: dict[str, Hex64]


class P3RenderBinding(StrictModel):
    side: SideName
    path: str
    sha256: Hex64


class P3QcRow(StrictModel):
    side: SideName
    integrated_mlufs: int | None
    peak_mb: int
    channels: int
    issues: tuple[str, ...]


class P3BuildObservation(StrictModel):
    """Raw per-snapshot observation; the evaluator recomputes every claim."""

    schema_version: Literal["p3-build-observation-v1"] = "p3-build-observation-v1"
    snapshot_id: SnapshotId
    work_dir: str
    route: str
    structure: tuple[P3StructRow, ...]
    presentation: P3PresentationHashes
    renders: tuple[P3RenderBinding, ...]
    parity_passed: bool
    qc: tuple[P3QcRow, ...]
    readback_verified: bool
    used_asset_sha256: dict[str, Hex64]
    rights_reasons: dict[str, str]


class P3RegressionObservation(StrictModel):
    """The Phase-2 regression rerun: raw exit + parsed gate result."""

    schema_version: Literal["p3-regression-v1"] = "p3-regression-v1"
    evidence_dir: str
    exit_code: int = Field(strict=True)
    timed_out: bool
    result_path: str
    result_sha256: Hex64
    policy_sha256: Hex64
    criteria_passed: dict[str, bool]
    passed: bool


class P3ScanFinding(StrictModel):
    path: str
    line: int = Field(strict=True)
    snippet: str


class P3ScanRecord(StrictModel):
    """Static Builder inspection: scanned roots plus any Phase-4 imports."""

    schema_version: Literal["p3-scan-record-v1"] = "p3-scan-record-v1"
    channel_roots: tuple[str, ...]
    channel_findings: tuple[P3ScanFinding, ...]
    phase4_modules: tuple[str, ...]


class P3GateObservation(StrictModel):
    schema_version: Literal["p3-gate-observation-v1"] = "p3-gate-observation-v1"
    builds: tuple[P3BuildObservation, ...]
    regression: P3RegressionObservation
    scan: P3ScanRecord


RESULT_NAME = "gate-result.json"
OBSERVATION_NAME = "observation.json"
PARITY_REPORT_NAME = "parity-report.json"
AB_DIR_NAME = "ab"
BUILDS_DIR_NAME = "builds"
REGRESSION_DIR_NAME = "phase-2-regression"
SCAN_NAME = "builder-scan.json"


__all__ = [
    "AB_DIR_NAME",
    "BUILDS_DIR_NAME",
    "OBSERVATION_NAME",
    "PARITY_REPORT_NAME",
    "REGRESSION_DIR_NAME",
    "RESULT_NAME",
    "SCAN_NAME",
    "P3BuildObservation",
    "P3GateObservation",
    "P3PresentationHashes",
    "P3QcRow",
    "P3RegressionObservation",
    "P3RenderBinding",
    "P3ScanFinding",
    "P3ScanRecord",
    "P3StructRow",
    "SideName",
    "SnapshotId",
]
