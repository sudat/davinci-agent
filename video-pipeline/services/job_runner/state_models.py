"""Strict runtime-state models for the job runner SQLite store.

The database holds ONLY runtime/control state (PRD 7.3): job progress,
stage-run bookkeeping, SQL lease rows, approval references and cache
pointers. Committed Plan bodies and every editorial Artifact stay
canonical FILES in the content-addressed artifact store; rows carry
hash pointers only, and every integer sequence field is a logical
counter owned by the store, never a wall-clock reading.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, TypeAdapter

from services.contracts.primitives import (
    ArtifactId,
    Identifier,
    Sha256,
    StrictModel,
)
from services.job_runner.state_errors import StateStoreError

JobStatus = Literal[
    "CREATED",
    "INGESTED",
    "NORMALIZED",
    "ANALYZED",
    "PLAN_PROPOSED",
    "PLAN_COMMITTED",
    "PREVIEW_READY",
    "EDITORIAL_APPROVED",
    "RESOLVE_BUILT",
    "QC_PASSED",
    "FINAL_APPROVED",
    "FROZEN",
]

StageRunStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed_blocked",
    "failed_retryable",
]


class JobRow(StrictModel):
    job_id: Identifier
    episode_id: Identifier
    current_stage: Identifier
    status: JobStatus
    created_at_seq: int = Field(ge=1, strict=True)
    updated_at_seq: int = Field(ge=1, strict=True)


class StageRunRow(StrictModel):
    job_id: Identifier
    stage_name: Identifier
    input_artifact_hashes: tuple[Sha256, ...]
    adopted_artifact_hash: Sha256 | None = None
    status: StageRunStatus
    idempotency_key: Identifier
    retry_count: int = Field(default=0, ge=0, strict=True)
    last_error_code: Identifier | None = None


class LeaseRow(StrictModel):
    """SQL lease row complementing the flock authority.

    ``expires_at`` is compared ONLY against caller-supplied ``now``
    values (a logical test clock or an epoch reading); this module never
    reads the wall clock, so lease behaviour is fully deterministic
    under test and replay.
    """

    resource: Identifier
    holder: Identifier
    expires_at: int = Field(ge=0, strict=True)


class ApprovalRefRow(StrictModel):
    purpose: Identifier
    target_hash: Sha256
    artifact_ref: ArtifactId
    recorded_seq: int = Field(ge=1, strict=True)


class CachePointerRow(StrictModel):
    cache_key: Identifier
    artifact_hash: Sha256
    producer_version: Identifier


class JobSnapshot(StrictModel):
    job: JobRow
    stage_runs: tuple[StageRunRow, ...]


class JobRecovery(StrictModel):
    """Resume bookkeeping derived from committed runtime state only.

    ``resume_input_hashes`` are the recorded inputs of the last
    committed (succeeded and adopted) stage run, so the Stage Runner can
    resume from committed inputs; this is a query result, not execution.
    """

    job_id: Identifier
    job_status: JobStatus
    current_stage: Identifier
    last_succeeded_stage: Identifier | None
    resume_input_hashes: tuple[Sha256, ...]
    last_adopted_hash: Sha256 | None


class VerificationReport(StrictModel):
    checked_stage_runs: int = Field(ge=0, strict=True)
    checked_hashes: int = Field(ge=0, strict=True)
    checked_approval_refs: int = Field(ge=0, strict=True)
    checked_cache_pointers: int = Field(ge=0, strict=True)


class PragmaState(StrictModel):
    journal_mode: str
    foreign_keys: bool
    synchronous: int


_HASH_LIST = TypeAdapter(list[Sha256])


def text_column(value: object) -> str:
    """Narrow a SQLite column value to ``str`` or fail closed."""

    if not isinstance(value, str):
        raise StateStoreError(
            "row-decode", f"expected TEXT column value, got {type(value).__name__}"
        )
    return value


def int_column(value: object) -> int:
    """Narrow a SQLite column value to ``int`` or fail closed."""

    if not isinstance(value, int):
        raise StateStoreError(
            "row-decode", f"expected INTEGER column value, got {type(value).__name__}"
        )
    return value


def encode_hash_list(hashes: tuple[Sha256, ...]) -> str:
    """Encode input-artifact hashes as a canonical JSON string column."""

    return json.dumps(list(hashes), separators=(",", ":"), ensure_ascii=False)


def decode_hash_list(raw: str) -> tuple[Sha256, ...]:
    """Decode a canonical JSON hash column, validating every sha256."""

    return tuple(_HASH_LIST.validate_json(raw.encode()))


__all__ = [
    "ApprovalRefRow",
    "CachePointerRow",
    "JobRecovery",
    "JobRow",
    "JobSnapshot",
    "JobStatus",
    "LeaseRow",
    "PragmaState",
    "StageRunRow",
    "StageRunStatus",
    "VerificationReport",
    "decode_hash_list",
    "encode_hash_list",
    "int_column",
    "text_column",
]
