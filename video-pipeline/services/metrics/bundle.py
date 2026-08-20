"""Load a metrics event bundle directory and hash-bind every input.

The bundle is the unit of truth: fixed file names, real recorded-event
formats, full chain verification. Malformed or tampered input fails
closed with a typed ``MetricsBundleError``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from services.approvals.chain_key import CHAIN_KEY_FILE_SUFFIX, ChainKeyError, load_chain_key
from services.approvals.models import ChainedOperationRecord
from services.approvals.verify import VerificationError, validate_supersession_chain
from services.foundation_io import sha256_file
from services.job_runner.stage_runner_models import GENESIS_HASH, hash_event
from services.metrics.models import (
    AuthoredClaim,
    ClaimsFile,
    EpisodeDeclaration,
    EpisodesFile,
    TimingEvent,
)
from services.metrics.report_models import BundleFileBinding
from services.metrics.streams import (
    ArtifactInventoryRecord,
    QcOutcomeRecord,
    TraceBuildItem,
    TraceDecision,
    TraceSource,
    WrappedReviewEvent,
    WrappedStageEvent,
)
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    EventStreamError,
    compute_event_id,
    event_proposal,
)

EPISODES_FILE: Final = "episodes.json"
TIMING_FILE: Final = "timing-events.jsonl"
REVIEW_FILE: Final = "review-events.jsonl"
OPERATIONS_FILE: Final = "operation-records.jsonl"
STAGE_FILE: Final = "stage-events.jsonl"
QC_FILE: Final = "qc-outcomes.jsonl"
INVENTORY_FILE: Final = "artifact-inventory.jsonl"
TRACE_SOURCES_FILE: Final = "trace-sources.jsonl"
TRACE_DECISIONS_FILE: Final = "trace-decisions.jsonl"
TRACE_ITEMS_FILE: Final = "trace-build-items.jsonl"
CLAIMS_FILE: Final = "claims.json"

BUNDLE_FILES: Final[tuple[str, ...]] = (
    EPISODES_FILE,
    TIMING_FILE,
    REVIEW_FILE,
    OPERATIONS_FILE,
    STAGE_FILE,
    QC_FILE,
    INVENTORY_FILE,
    TRACE_SOURCES_FILE,
    TRACE_DECISIONS_FILE,
    TRACE_ITEMS_FILE,
    CLAIMS_FILE,
)


class MetricsBundleError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class EventBundle:
    root: Path
    episodes: tuple[EpisodeDeclaration, ...]
    timing_events: tuple[TimingEvent, ...]
    review_events: tuple[WrappedReviewEvent, ...]
    operation_records: tuple[ChainedOperationRecord, ...]
    stage_events: tuple[WrappedStageEvent, ...]
    qc_outcomes: tuple[QcOutcomeRecord, ...]
    inventory: tuple[ArtifactInventoryRecord, ...]
    trace_sources: tuple[TraceSource, ...]
    trace_decisions: tuple[TraceDecision, ...]
    trace_build_items: tuple[TraceBuildItem, ...]
    claims: tuple[AuthoredClaim, ...]
    bindings: tuple[BundleFileBinding, ...]


def _fail(code: str, detail: str) -> MetricsBundleError:
    return MetricsBundleError(code, detail)


def _json_object(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise _fail("bundle-malformed", f"{path.name}: {error}") from error


def _jsonl(path: Path) -> list[object]:
    rows: list[object] = []
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as error:
        raise _fail("bundle-malformed", f"{path.name}: {error}") from error
    return rows


def _load_simple[Model: BaseModel](path: Path, model: type[Model]) -> tuple[Model, ...]:
    if not path.exists():
        return ()
    try:
        return tuple(model.model_validate(row) for row in _jsonl(path))
    except ValidationError as error:
        raise _fail("bundle-malformed", f"{path.name}: {error}") from error


def _load_review(path: Path) -> tuple[WrappedReviewEvent, ...]:
    rows = []
    for index, row in enumerate(_jsonl(path), start=1):
        try:
            wrapped = WrappedReviewEvent.model_validate(row)
            event_proposal(wrapped.event)
            rows.append(wrapped)
        except (ValidationError, EventStreamError) as error:
            raise _fail("bundle-malformed", f"{path.name}: line {index}: {error}") from error
    previous = GENESIS_EVENT_HASH
    for index, wrapped in enumerate(rows, start=1):
        event = wrapped.event
        if event.sequence != index or event.previous_event_hash != previous:
            raise _fail("review-chain", f"{path.name}: broken chain at line {index}")
        if event.event_id != compute_event_id(event):
            raise _fail("review-chain", f"{path.name}: event id drift at line {index}")
        previous = event.event_id
    return tuple(rows)


def _load_stage(path: Path) -> tuple[WrappedStageEvent, ...]:
    rows = []
    for index, row in enumerate(_jsonl(path), start=1):
        try:
            rows.append(WrappedStageEvent.model_validate(row))
        except ValidationError as error:
            raise _fail("bundle-malformed", f"{path.name}: line {index}: {error}") from error
    previous = GENESIS_HASH
    for index, wrapped in enumerate(rows, start=1):
        event = wrapped.event
        if event.sequence != index or event.previous_event_hash != previous:
            raise _fail("stage-chain", f"{path.name}: broken chain at line {index}")
        if event.event_hash != hash_event(event):
            raise _fail("stage-chain", f"{path.name}: event hash drift at line {index}")
        previous = event.event_hash
    return tuple(rows)


def _load_operations(path: Path) -> tuple[ChainedOperationRecord, ...]:
    records: list[ChainedOperationRecord] = []
    for index, row in enumerate(_jsonl(path), start=1):
        try:
            records.append(ChainedOperationRecord.model_validate(row))
        except ValidationError as error:
            raise _fail("operations-invalid", f"{path.name}: line {index}: {error}") from error
    if not records:
        return ()
    try:
        chain_key = load_chain_key(path, create=False)
        validate_supersession_chain(tuple(records), chain_key=chain_key)
    except (ChainKeyError, VerificationError) as error:
        raise _fail("operations-chain", f"{path.name}: {error}") from error
    return tuple(records)


def load_bundle(root: Path) -> EventBundle:
    if not root.is_dir():
        raise _fail("bundle-missing", f"event bundle directory not found: {root}")
    allowed = (*BUNDLE_FILES, OPERATIONS_FILE + CHAIN_KEY_FILE_SUFFIX)
    unexpected = sorted(
        entry.name for entry in root.iterdir() if entry.name not in allowed
    )
    if unexpected:
        raise _fail("unexpected-file", f"unknown bundle entries: {unexpected}")
    try:
        episodes_file = EpisodesFile.model_validate(_json_object(root / EPISODES_FILE))
    except ValidationError as error:
        raise _fail("episodes-invalid", f"{EPISODES_FILE}: {error}") from error

    claims_path = root / CLAIMS_FILE
    claims: tuple[AuthoredClaim, ...] = ()
    if claims_path.exists():
        try:
            claims = ClaimsFile.model_validate(_json_object(claims_path)).claims
        except ValidationError as error:
            raise _fail("claims-invalid", f"{CLAIMS_FILE}: {error}") from error

    bindings = tuple(
        BundleFileBinding(
            name=name,
            sha256=sha256_file(root / name) if (root / name).is_file() else None,
        )
        for name in BUNDLE_FILES
    )
    review_events: tuple[WrappedReviewEvent, ...] = ()
    if (root / REVIEW_FILE).exists():
        review_events = _load_review(root / REVIEW_FILE)
    operation_records: tuple[ChainedOperationRecord, ...] = ()
    if (root / OPERATIONS_FILE).exists():
        operation_records = _load_operations(root / OPERATIONS_FILE)
    stage_events: tuple[WrappedStageEvent, ...] = ()
    if (root / STAGE_FILE).exists():
        stage_events = _load_stage(root / STAGE_FILE)
    return EventBundle(
        root=root,
        episodes=tuple(episodes_file.episodes),
        timing_events=_load_simple(root / TIMING_FILE, TimingEvent),
        review_events=review_events,
        operation_records=operation_records,
        stage_events=stage_events,
        qc_outcomes=_load_simple(root / QC_FILE, QcOutcomeRecord),
        inventory=_load_simple(root / INVENTORY_FILE, ArtifactInventoryRecord),
        trace_sources=_load_simple(root / TRACE_SOURCES_FILE, TraceSource),
        trace_decisions=_load_simple(root / TRACE_DECISIONS_FILE, TraceDecision),
        trace_build_items=_load_simple(root / TRACE_ITEMS_FILE, TraceBuildItem),
        claims=claims,
        bindings=bindings,
    )


__all__ = [
    "BUNDLE_FILES",
    "CLAIMS_FILE",
    "EPISODES_FILE",
    "INVENTORY_FILE",
    "OPERATIONS_FILE",
    "QC_FILE",
    "REVIEW_FILE",
    "STAGE_FILE",
    "TIMING_FILE",
    "TRACE_DECISIONS_FILE",
    "TRACE_ITEMS_FILE",
    "TRACE_SOURCES_FILE",
    "EventBundle",
    "MetricsBundleError",
    "load_bundle",
]
