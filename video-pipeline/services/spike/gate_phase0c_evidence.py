"""Raw evidence loading for one Phase-0C gate case.

``load_case_evidence`` reads the case's store versions/events, translator
record, and preview traces, verifying every hash the store records; any
missing, unparsable, or drifted artifact raises ``EvidenceLoadError`` (the
evaluator turns that into an unclassified Stop, never a pass).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.foundation_io import sha256_file
from services.preview.models import PreviewTraceManifest
from services.review_command.store import (
    PlanVersionsIndex,
    ReviewCommitError,
    load_events,
    load_index,
    load_version_plan,
)
from services.review_command.translator_record import TranslatorRecord
from services.spike.gate_phase0c_bindings import load_ir_file
from services.spike.gate_phase0c_case import load_case
from services.spike.gate_phase0c_models import (
    EVENTS_LOG_NAME,
    preview_dir,
    store_dir,
    translator_record_path,
)

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C
    from services.fixtures.manifest_phase0c import Phase0CFixtureManifest
    from services.review_command.events import ReviewEvent0C

TRACE_NAME: Final = "preview-trace.json"
INDEX_NAME: Final = "versions.json"


class EvidenceLoadError(Exception):
    """A raw 0C evidence artifact is missing, unparsable, or hash-drifted."""


@dataclass(frozen=True, slots=True)
class CaseEvidence:
    fixture_id: str
    manifest: Phase0CFixtureManifest
    events: tuple[ReviewEvent0C, ...]
    plan_base: EditPlan0C
    plan_head: EditPlan0C
    head_version: int
    ir_base: TimelineIr0C
    ir_head: TimelineIr0C
    ir_head_sha256: str
    record: TranslatorRecord
    trace0: PreviewTraceManifest
    trace1: PreviewTraceManifest | None
    raw_shas: tuple[str, ...]
    preview1_dir: Path


def _load_trace(directory: Path) -> PreviewTraceManifest:
    try:
        trace = PreviewTraceManifest.model_validate_json((directory / TRACE_NAME).read_bytes())
        preview = Path(trace.preview.path)
        if (
            not preview.is_file()
            or sha256_file(preview) != trace.preview.sha256
            or preview.stat().st_size != trace.preview.size
        ):
            raise EvidenceLoadError(f"preview bytes do not match the trace: {preview}")
    except (OSError, ValidationError) as error:
        raise EvidenceLoadError(f"trace evidence invalid at {directory}: {error}") from error
    return trace


def _load_ir(store: Path, index: PlanVersionsIndex, version: int) -> TimelineIr0C:
    path = store / f"ir-v{version}.json"
    try:
        if sha256_file(path) != index.versions[str(version)].ir_sha256:
            raise EvidenceLoadError(f"ir-v{version} bytes drift from the store index")
        return load_ir_file(path)
    except ValidationError as error:
        raise EvidenceLoadError(f"ir evidence unparsable: {path}: {error}") from error
    except OSError as error:
        raise EvidenceLoadError(f"ir evidence invalid: {path}: {error}") from error


def load_case_evidence(evidence: Path, fixture_id: str) -> CaseEvidence:
    try:
        manifest = load_case(fixture_id)
        store = store_dir(evidence, fixture_id)
        index = load_index(store)
        events = load_events(store / EVENTS_LOG_NAME)
        plan_base = load_version_plan(store, index, 1)
        head_version = len(index.versions)
        plan_head = load_version_plan(store, index, head_version)
        ir_base = _load_ir(store, index, 1)
        ir_head = _load_ir(store, index, head_version)
        record = TranslatorRecord.model_validate_json(
            translator_record_path(evidence, fixture_id).read_bytes()
        )
        trace0 = _load_trace(preview_dir(evidence, fixture_id, 0))
    except (OSError, ValidationError, ReviewCommitError) as error:
        raise EvidenceLoadError(f"{fixture_id}: {error}") from error
    generation1 = preview_dir(evidence, fixture_id, 1)
    trace1 = _load_trace(generation1) if generation1.is_dir() else None
    ir_path = store / f"ir-v{head_version}.json"
    shas = [
        sha256_file(path)
        for path in (
            store / INDEX_NAME,
            store / EVENTS_LOG_NAME,
            ir_path,
            translator_record_path(evidence, fixture_id),
            preview_dir(evidence, fixture_id, 0) / TRACE_NAME,
        )
        if path.is_file()
    ]
    return CaseEvidence(
        fixture_id=fixture_id,
        manifest=manifest,
        events=events,
        plan_base=plan_base,
        plan_head=plan_head,
        head_version=head_version,
        ir_base=ir_base,
        ir_head=ir_head,
        ir_head_sha256=sha256_file(ir_path),
        record=record,
        trace0=trace0,
        trace1=trace1,
        raw_shas=tuple(shas),
        preview1_dir=generation1,
    )


__all__ = [
    "CaseEvidence",
    "EvidenceLoadError",
    "load_case_evidence",
]
