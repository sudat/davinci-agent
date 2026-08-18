"""Versioned Edit Plan store: versions index + sealed event log (Todo 43).

The Todo-41 store family with ``plan_type=edit``: an immutable
``versions.json`` chain index (version → plan sha, parent version, event id)
over the SAME sealed JSONL event log format (``events.jsonl`` + head seal,
reused verbatim from the Todo-30/41 machinery — no format fork). Version
files are the content-addressed objects of the artifact store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import ArtifactId, Identifier, Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes
from services.review_command.events import GENESIS_EVENT_HASH, EventSeal
from services.validate.selection_plan_store import (
    INDEX_NAME,
    append_events,
    event_log_path,
    load_events,
    seal_path,
)


class EditPlanStoreError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class EditPlanVersionEntry(StrictModel):
    """One committed version: plan sha, parent version, and commit event."""

    plan_sha256: Sha256
    plan_artifact_id: ArtifactId
    proposal_sha256: Sha256
    parent_version: int | None = Field(default=None, ge=1, strict=True)
    event_id: Sha256


class EditPlanVersionsIndex(StrictModel):
    schema_version: Literal["edit-plan-versions-v1"]
    episode_id: Identifier
    base_version: Identifier
    versions: dict[str, EditPlanVersionEntry]

    @model_validator(mode="after")
    def require_contiguous_chain(self) -> EditPlanVersionsIndex:
        for position in range(1, len(self.versions) + 1):
            entry = self.versions.get(str(position))
            parent = None if position == 1 else position - 1
            if entry is None or entry.parent_version != parent:
                raise PydanticCustomError(
                    "versions", "version chain broken at {position}", {"position": position}
                )
        return self


def initialize_edit_plan_store(
    edit_plan_dir: Path, *, episode_id: str, base_version: str
) -> None:
    """Register an empty edit plan store; idempotent per episode and base."""

    edit_plan_dir.mkdir(parents=True, exist_ok=True)
    index_path = edit_plan_dir / INDEX_NAME
    if index_path.exists():
        index = load_edit_index(edit_plan_dir)
        if index.episode_id == episode_id and index.base_version == base_version:
            return
        raise EditPlanStoreError(
            "store_init_conflict",
            "edit plan store already initialized for a different episode or base",
        )
    atomic_write(event_log_path(edit_plan_dir), b"")
    atomic_write(
        seal_path(edit_plan_dir),
        canonical_model_bytes(EventSeal(sequence=0, event_id=GENESIS_EVENT_HASH)),
    )
    atomic_write(
        index_path,
        canonical_model_bytes(
            EditPlanVersionsIndex(
                schema_version="edit-plan-versions-v1",
                episode_id=episode_id,
                base_version=base_version,
                versions={},
            )
        ),
    )


def load_edit_index(edit_plan_dir: Path) -> EditPlanVersionsIndex:
    try:
        return EditPlanVersionsIndex.model_validate_json(
            (edit_plan_dir / INDEX_NAME).read_bytes()
        )
    except OSError as error:
        raise EditPlanStoreError("store_not_initialized", str(error)) from error
    except ValidationError as error:
        raise EditPlanStoreError("invalid_index", str(error)) from error


def latest_edit_version(index: EditPlanVersionsIndex) -> int:
    return len(index.versions)


def current_edit_base_label(index: EditPlanVersionsIndex) -> str:
    """The base a NEW proposal must declare: the genesis base or the latest label."""

    latest = latest_edit_version(index)
    return index.base_version if latest == 0 else f"v{latest}"


def write_edit_entry(
    edit_plan_dir: Path,
    index: EditPlanVersionsIndex,
    version: int,
    entry: EditPlanVersionEntry,
) -> EditPlanVersionsIndex:
    """Persist one new version entry; an existing identical entry is a no-op."""

    existing = index.versions.get(str(version))
    if existing is not None:
        if existing != entry:
            raise EditPlanStoreError(
                "version_exists",
                f"version v{version} already exists with different bytes",
            )
        return index
    updated = EditPlanVersionsIndex(
        schema_version=index.schema_version,
        episode_id=index.episode_id,
        base_version=index.base_version,
        versions=index.versions | {str(version): entry},
    )
    atomic_write(edit_plan_dir / INDEX_NAME, canonical_model_bytes(updated))
    return updated


__all__ = [
    "INDEX_NAME",
    "EditPlanStoreError",
    "EditPlanVersionEntry",
    "EditPlanVersionsIndex",
    "append_events",
    "current_edit_base_label",
    "initialize_edit_plan_store",
    "latest_edit_version",
    "load_edit_index",
    "load_events",
]
