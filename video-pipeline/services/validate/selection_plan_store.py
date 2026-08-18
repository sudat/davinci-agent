"""Versioned Selection Plan store: versions index + sealed event log.

The Todo-30 store pattern adapted to selection plans: an immutable
``versions.json`` chain index (version → plan sha, parent version,
event id) plus the sealed JSONL commit log. Version files are the
content-addressed objects of the artifact store, keyed by plan sha.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.artifact_registry.store_view import read_file_nofollow
from services.contracts.primitives import ArtifactId, Identifier, Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes
from services.review_command.events import GENESIS_EVENT_HASH, EventSeal
from services.validate.selection_events import (
    SelectionCommitEvent,
    SelectionEventStreamError,
    parse_selection_event_stream,
    seal_for,
)

INDEX_NAME = "versions.json"
EVENT_LOG_NAME = "events.jsonl"


class SelectionPlanStoreError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class SelectionPlanVersionEntry(StrictModel):
    """One committed version: plan sha, parent version, and commit event."""

    plan_sha256: Sha256
    plan_artifact_id: ArtifactId
    proposal_sha256: Sha256
    parent_version: int | None = Field(default=None, ge=1, strict=True)
    event_id: Sha256


class SelectionPlanVersionsIndex(StrictModel):
    schema_version: Literal["selection-plan-versions-v1"]
    episode_id: Identifier
    base_version: Identifier
    versions: dict[str, SelectionPlanVersionEntry]

    @model_validator(mode="after")
    def require_contiguous_chain(self) -> SelectionPlanVersionsIndex:
        for position in range(1, len(self.versions) + 1):
            entry = self.versions.get(str(position))
            parent = None if position == 1 else position - 1
            if entry is None or entry.parent_version != parent:
                raise PydanticCustomError(
                    "versions",
                    "version chain broken at {position}",
                    {"position": position},
                )
        return self


def event_log_path(plan_dir: Path) -> Path:
    return plan_dir / EVENT_LOG_NAME


def seal_path(plan_dir: Path) -> Path:
    log = event_log_path(plan_dir)
    return log.parent / f"{log.name}.seal"


def initialize_plan_store(
    plan_dir: Path, *, episode_id: str, base_version: str
) -> None:
    """Register an empty plan store; idempotent per episode and base label."""

    plan_dir.mkdir(parents=True, exist_ok=True)
    index_path = plan_dir / INDEX_NAME
    if index_path.exists():
        index = load_index(plan_dir)
        if index.episode_id == episode_id and index.base_version == base_version:
            return
        raise SelectionPlanStoreError(
            "store_init_conflict",
            "plan store already initialized for a different episode or base",
        )
    atomic_write(event_log_path(plan_dir), b"")
    atomic_write(
        seal_path(plan_dir),
        canonical_model_bytes(EventSeal(sequence=0, event_id=GENESIS_EVENT_HASH)),
    )
    index = SelectionPlanVersionsIndex(
        schema_version="selection-plan-versions-v1",
        episode_id=episode_id,
        base_version=base_version,
        versions={},
    )
    atomic_write(index_path, canonical_model_bytes(index))


def load_index(plan_dir: Path) -> SelectionPlanVersionsIndex:
    try:
        return SelectionPlanVersionsIndex.model_validate_json(
            (plan_dir / INDEX_NAME).read_bytes()
        )
    except OSError as error:
        raise SelectionPlanStoreError("store_not_initialized", str(error)) from error
    except ValidationError as error:
        raise SelectionPlanStoreError("invalid_index", str(error)) from error


def load_events(plan_dir: Path) -> tuple[SelectionCommitEvent, ...]:
    try:
        raw_log = read_file_nofollow(event_log_path(plan_dir)) or b""
        seal_raw = read_file_nofollow(seal_path(plan_dir))
    except OSError as error:
        raise SelectionPlanStoreError("broken_seal", str(error)) from error
    if seal_raw is None:
        raise SelectionPlanStoreError("broken_seal", f"missing seal {seal_path(plan_dir)}")
    try:
        events = parse_selection_event_stream(raw_log)
        seal = EventSeal.model_validate_json(seal_raw)
    except (SelectionEventStreamError, ValidationError) as error:
        raise SelectionPlanStoreError("broken_stream", str(error)) from error
    expected = seal_for(events)
    if seal.sequence != expected.sequence or seal.event_id != expected.event_id:
        raise SelectionPlanStoreError(
            "broken_seal",
            f"seal claims sequence {seal.sequence}/{seal.event_id[:8]} but the log "
            f"ends at {expected.sequence}/{expected.event_id[:8]}",
        )
    return events


def append_events(plan_dir: Path, additions: tuple[SelectionCommitEvent, ...]) -> None:
    """Append sealed events: fsync the log, then atomically advance the seal."""

    if not additions:
        return
    log = event_log_path(plan_dir)
    with log.open("ab") as stream:
        for event in additions:
            stream.write(canonical_model_bytes(event) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    atomic_write(seal_path(plan_dir), canonical_model_bytes(seal_for(additions)))


def latest_version(index: SelectionPlanVersionsIndex) -> int:
    return len(index.versions)


def current_base_label(index: SelectionPlanVersionsIndex) -> str:
    """The base a NEW proposal must declare: the genesis base or the latest v-label."""

    latest = latest_version(index)
    return index.base_version if latest == 0 else f"v{latest}"


def write_entry(
    plan_dir: Path,
    index: SelectionPlanVersionsIndex,
    version: int,
    entry: SelectionPlanVersionEntry,
) -> SelectionPlanVersionsIndex:
    """Persist one new version entry; an existing identical entry is a no-op."""

    existing = index.versions.get(str(version))
    if existing is not None:
        if existing != entry:
            raise SelectionPlanStoreError(
                "version_exists",
                f"version v{version} already exists with different bytes",
            )
        return index
    updated = SelectionPlanVersionsIndex(
        schema_version=index.schema_version,
        episode_id=index.episode_id,
        base_version=index.base_version,
        versions=index.versions | {str(version): entry},
    )
    atomic_write(plan_dir / INDEX_NAME, canonical_model_bytes(updated))
    return updated


def entry_for(index: SelectionPlanVersionsIndex, version: int) -> SelectionPlanVersionEntry:
    entry = index.versions.get(str(version))
    if entry is None:
        raise SelectionPlanStoreError(
            "unknown_version", f"version v{version} is not in the versions index"
        )
    return entry


__all__ = [
    "EVENT_LOG_NAME",
    "INDEX_NAME",
    "SelectionPlanStoreError",
    "SelectionPlanVersionEntry",
    "SelectionPlanVersionsIndex",
    "append_events",
    "current_base_label",
    "entry_for",
    "event_log_path",
    "initialize_plan_store",
    "latest_version",
    "load_events",
    "load_index",
    "seal_path",
    "write_entry",
]
