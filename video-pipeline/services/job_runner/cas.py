"""Compare-and-set authority for Job State transitions (Todo 10).

``apply_transition`` is the single serialized writer for job status and
the job's adopted artifact hash. Everything happens inside ONE sqlite
``BEGIN IMMEDIATE`` transaction on the store's connection: the current
row is re-read (caller-supplied expectations are never trusted), a
mismatch on status or adopted parent hash raises ``superseded`` with no
write (PRD stale-work suppression), an edge outside the transition
table raises ``forbidden-transition`` — or ``forbidden-approval-required``
for approval-gate bypasses and unpayloaded gated edges — and only then
are the new status, adopted-hash pointer, and ``updated_at_seq`` written
atomically. There is no distributed transaction here; crash recovery is
caller re-run: the CAS is idempotent for identical replays.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Final, cast, get_args

from pydantic import TypeAdapter
from pydantic_core import ValidationError

from services.contracts.primitives import ArtifactId, Identifier, Sha256, StrictModel
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_models import CachePointerRow, JobStatus, text_column
from services.job_runner.transitions import TransitionEdge, edge_for, is_gate_bypass

if TYPE_CHECKING:
    from services.job_runner.state_store import StateStore


class ApprovalRef(StrictModel):
    """Operator-checkpoint reference consumed by an approval-gated edge."""

    purpose: Identifier
    target_hash: Sha256
    artifact_ref: ArtifactId


class TransitionPayload(StrictModel):
    """CAS payload; gated edges require ``approval``."""

    approval: ApprovalRef | None = None


class CasSnapshot(StrictModel):
    job_id: Identifier
    status: JobStatus
    adopted_artifact_hash: Sha256 | None
    updated_at_seq: int


class CasError(StateStoreError):
    """Typed CAS refusal: superseded / forbidden-transition / approval."""


_ADOPTED_POINTER_PREFIX: Final = "state-adopted"
_CAS_PRODUCER_VERSION: Final = "job-state-cas-v1"
_SHA256_ADAPTER: Final[TypeAdapter[Sha256]] = TypeAdapter(Sha256)
_STATUSES: Final[frozenset[str]] = frozenset(get_args(JobStatus))


def adopted_pointer_key(job_id: str) -> Identifier:
    """Cache-pointer key holding the job's currently adopted artifact."""

    return f"{_ADOPTED_POINTER_PREFIX}:{job_id}"


def current_job_state(store: StateStore, job_id: Identifier) -> CasSnapshot:
    """Read the job's current status and adopted artifact hash."""

    connection = store._connection  # noqa: SLF001 (same-package store; no public txn API)
    row = connection.execute(
        "SELECT status, updated_at_seq FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise StateStoreError("job-missing", f"no job row for {job_id}")
    status_text = text_column(row[0])
    if status_text not in _STATUSES:
        raise StateStoreError("row-decode", f"unknown job status {status_text!r}")
    pointer = store.get_cache_pointer(adopted_pointer_key(job_id))
    return CasSnapshot(
        job_id=job_id,
        status=cast("JobStatus", status_text),
        adopted_artifact_hash=None if pointer is None else pointer.artifact_hash,
        updated_at_seq=int(row[1]),
    )


def _require_sha256(value: str, label: str) -> Sha256:
    try:
        return _SHA256_ADAPTER.validate_python(value)
    except ValidationError as error:
        raise CasError("invalid-hash", f"{label} must be a sha256 hex digest") from error


def apply_transition(  # noqa: PLR0913 (single CAS entrypoint; kwargs are the contract)
    store: StateStore,
    job_id: Identifier,
    *,
    expected_status: JobStatus,
    expected_parent_hash: Sha256 | None,
    new_status: JobStatus,
    new_artifact_hash: Sha256,
    payload: TransitionPayload | None = None,
) -> CasSnapshot:
    """Atomically move the job across one legal transition edge."""

    new_hash = _require_sha256(new_artifact_hash, "new_artifact_hash")
    parent_hash = (
        None
        if expected_parent_hash is None
        else _require_sha256(expected_parent_hash, "expected_parent_hash")
    )
    connection = store._connection  # noqa: SLF001 (Todo-9 store exposes no txn surface)
    connection.execute("BEGIN IMMEDIATE")
    try:
        result, dirty = _apply_locked(
            connection,
            store,
            job_id,
            expected_status=expected_status,
            expected_parent_hash=parent_hash,
            new_status=new_status,
            new_artifact_hash=new_hash,
            payload=payload,
        )
    except Exception:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT" if dirty else "ROLLBACK")
    return result


def _apply_locked(  # noqa: PLR0913 (mirrors apply_transition; one locked step)
    connection: sqlite3.Connection,
    store: StateStore,
    job_id: str,
    *,
    expected_status: JobStatus,
    expected_parent_hash: Sha256 | None,
    new_status: JobStatus,
    new_artifact_hash: Sha256,
    payload: TransitionPayload | None,
) -> tuple[CasSnapshot, bool]:
    current = current_job_state(store, cast("Identifier", job_id))
    if (
        current.status != expected_status
        or current.adopted_artifact_hash != expected_parent_hash
    ):
        raise CasError(
            "superseded",
            f"job {job_id} is {current.status}/{current.adopted_artifact_hash},"
            f" caller expected {expected_status}/{expected_parent_hash}; no write",
        )
    edge = edge_for(expected_status, new_status)
    if edge is None:
        code = (
            "forbidden-approval-required"
            if is_gate_bypass(expected_status, new_status)
            else "forbidden-transition"
        )
        raise CasError(
            code,
            f"transition {expected_status} -> {new_status} of job {job_id}"
            " is not in the transition table",
        )
    if edge.requires_approval:
        _consume_approval(store, job_id, edge, expected_parent_hash, payload)
    if (
        edge.current_status == edge.target_status
        and current.adopted_artifact_hash == new_artifact_hash
    ):
        return current, False
    store.put_cache_pointer(
        CachePointerRow(
            cache_key=adopted_pointer_key(job_id),
            artifact_hash=new_artifact_hash,
            producer_version=_CAS_PRODUCER_VERSION,
        )
    )
    seq_row = connection.execute(
        "UPDATE state_clock SET next_seq = next_seq + 1 WHERE id = 1"
        " RETURNING next_seq - 1"
    ).fetchone()
    if seq_row is None:
        raise StateStoreError("state-clock-missing", "state_clock row 1 is absent")
    seq = int(seq_row[0])
    connection.execute(
        "UPDATE jobs SET status = ?, updated_at_seq = ? WHERE job_id = ?",
        (new_status, seq, job_id),
    )
    return (
        CasSnapshot(
            job_id=cast("Identifier", job_id),
            status=new_status,
            adopted_artifact_hash=new_artifact_hash,
            updated_at_seq=seq,
        ),
        True,
    )


def _consume_approval(
    store: StateStore,
    job_id: str,
    edge: TransitionEdge,
    expected_parent_hash: Sha256 | None,
    payload: TransitionPayload | None,
) -> None:
    purpose = edge.approval_purpose
    if purpose is None:
        raise CasError(
            "forbidden-transition", "gated edge lacks its approval purpose"
        )
    approval = None if payload is None else payload.approval
    if approval is None:
        raise CasError(
            "forbidden-approval-required",
            f"transition of job {job_id} is approval-gated for {purpose}"
            " but the payload carries no operator checkpoint approval ref",
        )
    if approval.purpose != purpose or approval.target_hash != expected_parent_hash:
        raise CasError(
            "approval-target-mismatch",
            f"approval {approval.purpose}/{approval.target_hash} does not match"
            f" the gated edge {purpose} targeting {expected_parent_hash}",
        )
    store.record_approval_ref(
        purpose=purpose,
        target_hash=approval.target_hash,
        artifact_ref=approval.artifact_ref,
    )


__all__ = [
    "ApprovalRef",
    "CasError",
    "CasSnapshot",
    "TransitionPayload",
    "adopted_pointer_key",
    "apply_transition",
    "current_job_state",
]
