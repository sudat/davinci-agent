"""Value models, artifact names, and paths for the Phase-0B gate runner.

All 0B evidence lives under ``<evidence>/runs/<variant>/{ingest,normalize,
conform-map,live-readback}``; the evaluator recomputes every stored number
from these raw artifacts, never from a companion index or an authored flag.
Temporal values are integers or integer pairs (no floats anywhere).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

from pydantic import Field

from services.contracts.primitives import Sha256, StrictModel
from services.gates.phase0b import PHASE_0B_VARIANTS

GATE0B_ID: Final = "phase-0b"
GATE0B_MARKER: Final = "phase-0b:"
GATE0B_MODULE: Final = "services.spike.run_gate"

INGEST_DIR: Final = "ingest"
NORMALIZE_DIR: Final = "normalize"
MAP_DIR: Final = "conform-map"
READBACK_DIR: Final = "live-readback"
SOURCE_MANIFEST_NAME: Final = "source-manifest.json"
NORMALIZE_RECORD_NAME: Final = "normalize-record.json"
CONFORM_MAP_NAME: Final = "conform-map.json"
READBACK_REPORT_NAME: Final = "readback-report.json"
SYNC_NAME: Final = "sync-measurements.json"
RESULT_NAME: Final = "gate-result.json"
MATRIX0B_NAME: Final = "capability-matrix-0b.json"
EDIT_SOURCES_DIR: Final = "edit-sources"

DEFAULT_LOCK: Final = Path("config/toolchains/phase-0b-v1.json")
MANIFEST_DIR: Final = Path("tests/fixtures/manifests/phase-0b")
GOLDEN_DIR: Final = Path("tests/goldens/reference/phase-0b")
PARENT_DIR: Final = "phase-0a"

READBACK_TIMEOUT_SECONDS: Final = 300.0
ANCHOR_WINDOW_FRAMES: Final = 30
TIMELINE_RATE_NUM: Final = 30
TIMELINE_RATE_DEN: Final = 1
SAMPLES_PER_SECOND: Final = 48000

CRITERION_GOLDEN: Final = "phase-0b-required-variants-golden-match-100-percent"
CRITERION_DROPS: Final = "phase-0b-drop-dup-accounting-exact"
CRITERION_ANCHORS: Final = "phase-0b-subtitle-audio-anchor-tolerance"
CRITERION_READBACK: Final = "phase-0b-resolve-readback-exact"
CRITERION_SYNC: Final = "phase-0b-av-sync-within-one-timeline-frame"

STOP_STALE_BINDING: Final = "phase-0b-stop-stale-binding"
STOP_UNCLASSIFIED: Final = "phase-0b-stop-unclassified-readback-mismatch"

ANCHOR_PREFIXES: Final = (
    ("subtitle:", CRITERION_ANCHORS),
    ("audio:", CRITERION_ANCHORS),
    ("subtitle-audio:", CRITERION_ANCHORS),
)
SYNC_PREFIXES: Final = (
    ("marker:", CRITERION_SYNC),
    ("av-sync:", CRITERION_SYNC),
)


def variant_dir(evidence: Path, variant: str) -> Path:
    return evidence / "runs" / variant


def ingest_manifest_path(evidence: Path, variant: str) -> Path:
    return variant_dir(evidence, variant) / INGEST_DIR / SOURCE_MANIFEST_NAME


def normalize_record_path(evidence: Path, variant: str) -> Path:
    return variant_dir(evidence, variant) / NORMALIZE_DIR / NORMALIZE_RECORD_NAME


def conform_map_path(evidence: Path, variant: str) -> Path:
    return variant_dir(evidence, variant) / MAP_DIR / CONFORM_MAP_NAME


def readback_report_path(evidence: Path, variant: str) -> Path:
    return variant_dir(evidence, variant) / READBACK_DIR / READBACK_REPORT_NAME


def fixture_manifest_path(variant: str) -> Path:
    return MANIFEST_DIR / f"{variant}.json"


class RationalValue(StrictModel):
    num: int
    den: int = Field(gt=0)


class Placement(StrictModel):
    item_id: str = Field(min_length=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    record_start: int = Field(ge=0)


class ReadbackObservation(StrictModel):
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    record_start: int = Field(ge=0)
    record_end: int = Field(gt=0)
    media_path: str = Field(min_length=1)


class ReadbackItem(StrictModel):
    requested: Placement
    observed: ReadbackObservation

    @property
    def frame_delta(self) -> int:
        return max(
            abs(self.observed.source_start - self.requested.source_start),
            abs(self.observed.source_end - self.requested.source_end),
            abs(self.observed.record_start - (self.requested.record_start)),
            abs(self.observed.record_end - self.observed.record_start)
            - (self.requested.source_end - self.requested.source_start),
        )


class ReadbackBindings(StrictModel):
    host_report_sha256: Sha256
    fixture_manifest_sha256: Sha256
    source_manifest_sha256: Sha256
    normalize_record_sha256: Sha256
    edit_source_sha256: Sha256
    resolve_version: str = Field(min_length=1)
    resolve_build: str = Field(min_length=1)


class LiveReadbackReport(StrictModel):
    schema_version: Literal["phase-0b-readback-v1"]
    fixture_id: str = Field(min_length=1)
    bindings: ReadbackBindings
    timeline_rate_num: int = Field(gt=0)
    timeline_rate_den: int = Field(gt=0)
    frame_origin: int = Field(ge=0)
    items: tuple[ReadbackItem, ...] = Field(min_length=1)
    clip_properties: tuple[tuple[str, str], ...] = ()
    project_name: str = Field(min_length=1)

    @property
    def max_frame_delta(self) -> int:
        return max(item.frame_delta for item in self.items)


class SyncRow(StrictModel):
    variant: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: Literal["exact-zero", "tolerance-one-frame"]
    expected: int
    observed: int
    delta: RationalValue
    passed: bool


class SyncMeasurements(StrictModel):
    schema_version: Literal["phase-0b-sync-measurements-v1"]
    rows: tuple[SyncRow, ...] = Field(min_length=1)
    all_passed: bool


VARIANTS: Final = PHASE_0B_VARIANTS
