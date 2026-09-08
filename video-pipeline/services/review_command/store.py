"""Spike-local versioned plan store: sealed event log + immutable versions.

Persistence primitives shared by the commit writer: the versions.json chain
index, the sealed JSONL event log, tamper-checked version loading, and the
append path (fsync + atomic seal rename). No SQLite, no leases.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.edit_plan_0c import EditPlan0C
from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.review_command.events import (
    GENESIS_EVENT_HASH,
    RESTORED_EVENT_KIND,
    EventSeal,
    EventStreamError,
    ReviewEvent0C,
    _tuplize,
    parse_event_stream,
)
from services.review_command.reducer import version_ir

INDEX_NAME = "versions.json"


class ReviewCommitError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class VersionEntry(StrictModel):
    plan_sha256: Sha256
    ir_sha256: Sha256
    event_id: Sha256 | None = None
    parent_version: int | None = Field(default=None, ge=1, strict=True)


class PlanVersionsIndex(StrictModel):
    schema_version: Literal["plan-versions-v1"]
    base_artifact_id: Identifier
    versions: dict[str, VersionEntry]

    @model_validator(mode="after")
    def require_contiguous_chain(self) -> PlanVersionsIndex:
        for position in range(1, len(self.versions) + 1):
            entry = self.versions.get(str(position))
            parent = None if position == 1 else position - 1
            if entry is None or entry.parent_version != parent:
                raise PydanticCustomError(
                    "versions", "version chain broken at {position}", {"position": position}
                )
            if (position == 1) != (entry.event_id is None):
                raise PydanticCustomError(
                    "versions", "version {position} has a broken event binding",
                    {"position": position},
                )
        return self


class OperatorDecision0C(StrictModel):
    decision_id: str = Field(min_length=1, strict=True)
    actor_intent: Literal["operator", "model"]
    note: str | None = Field(default=None, min_length=1, strict=True)


@dataclass(frozen=True, slots=True)
class HeadState:
    plan: EditPlan0C
    base_plan: EditPlan0C
    version: int
    events: tuple[ReviewEvent0C, ...]
    index: PlanVersionsIndex


def seal_path(log_path: Path) -> Path:
    return log_path.parent / f"{log_path.name}.seal"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_index(plan_dir: Path) -> PlanVersionsIndex:
    try:
        return PlanVersionsIndex.model_validate_json((plan_dir / INDEX_NAME).read_bytes())
    except OSError as error:
        raise ReviewCommitError("store_not_initialized", str(error)) from error
    except ValidationError as error:
        raise ReviewCommitError("invalid_index", str(error)) from error


def load_events(log_path: Path) -> tuple[ReviewEvent0C, ...]:
    try:
        events = parse_event_stream(log_path.read_bytes())
        seal = EventSeal.model_validate_json(seal_path(log_path).read_bytes())
    except OSError as error:
        raise ReviewCommitError("broken_seal", str(error)) from error
    except (EventStreamError, ValidationError) as error:
        raise ReviewCommitError("broken_stream", str(error)) from error
    expected_sequence = events[-1].sequence if events else 0
    expected_id = events[-1].event_id if events else GENESIS_EVENT_HASH
    if seal.sequence != expected_sequence or seal.event_id != expected_id:
        raise ReviewCommitError(
            "broken_seal",
            f"seal claims sequence {seal.sequence}/{seal.event_id[:8]} "
            f"but the log ends at {expected_sequence}/{expected_id[:8]}",
        )
    return events


def append_events(log_path: Path, additions: tuple[ReviewEvent0C, ...]) -> None:
    with log_path.open("ab") as stream:
        for event in additions:
            stream.write(canonical_model_bytes(event) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    atomic_write(
        seal_path(log_path),
        canonical_model_bytes(
            EventSeal(sequence=additions[-1].sequence, event_id=additions[-1].event_id)
        ),
    )


def load_version_plan(plan_dir: Path, index: PlanVersionsIndex, version: int) -> EditPlan0C:
    entry = index.versions[str(version)]
    plan_path = plan_dir / f"plan-v{version}.json"
    ir_path = plan_dir / f"ir-v{version}.json"
    for path, expected in ((plan_path, entry.plan_sha256), (ir_path, entry.ir_sha256)):
        try:
            actual = sha256_file(path)
        except OSError as error:
            raise ReviewCommitError("foreign_plan", f"missing version file {path}") from error
        if actual != expected:
            raise ReviewCommitError(
                "foreign_plan",
                f"{path.name} bytes do not match the recorded version hash",
            )
    try:
        return EditPlan0C.model_validate(_tuplize(json.loads(plan_path.read_bytes())))
    except (ValidationError, ValueError) as error:
        raise ReviewCommitError("foreign_plan", f"unparsable plan version {version}") from error


def load_head(log_path: Path, plan_dir: Path) -> HeadState:
    index = load_index(plan_dir)
    events = load_events(log_path)
    version_creating = ("decision_applied", RESTORED_EVENT_KIND)
    applied = [event for event in events if event.kind in version_creating]
    results = {event.result_plan_version for event in applied}
    for version_label in index.versions:
        if int(version_label) > 1 and f"v{version_label}" not in results:
            raise ReviewCommitError(
                "invalid_index",
                f"version {version_label} references an event absent from the log",
            )
    for event in applied:
        label = event.result_plan_version
        if label is None or label[1:] not in index.versions:
            raise ReviewCommitError(
                "orphan_event",
                f"applied event for {label} has no committed version; run recover_orphan",
            )
    base_plan = load_version_plan(plan_dir, index, 1)
    head_version = len(index.versions)
    head_plan = load_version_plan(plan_dir, index, head_version)
    return HeadState(
        plan=head_plan,
        base_plan=base_plan,
        version=head_version,
        events=events,
        index=index,
    )


def initialize_store(base_plan: EditPlan0C, log_path: Path, plan_dir: Path) -> None:
    """Register the genesis version (v1) of a store; idempotent per base plan."""

    index_path = plan_dir / INDEX_NAME
    if index_path.exists():
        head = load_head(log_path, plan_dir)
        same_base = canonical_model_bytes(head.base_plan) == canonical_model_bytes(base_plan)
        if head.version == 1 and same_base:
            return
        raise ReviewCommitError("store_init_conflict", "store already initialized")
    plan_bytes = canonical_model_bytes(base_plan)
    ir_bytes = canonical_model_bytes(version_ir(base_plan, base_plan, 1))
    atomic_write(plan_dir / "plan-v1.json", plan_bytes)
    atomic_write(plan_dir / "ir-v1.json", ir_bytes)
    atomic_write(log_path, b"")
    atomic_write(
        seal_path(log_path),
        canonical_model_bytes(EventSeal(sequence=0, event_id=GENESIS_EVENT_HASH)),
    )
    index = PlanVersionsIndex(
        schema_version="plan-versions-v1",
        base_artifact_id=base_plan.artifact_id,
        versions={
            "1": VersionEntry(
                plan_sha256=sha256_bytes(plan_bytes), ir_sha256=sha256_bytes(ir_bytes)
            )
        },
    )
    atomic_write(index_path, canonical_model_bytes(index))
