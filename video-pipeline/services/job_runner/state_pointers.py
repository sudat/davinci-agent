"""Approval references and cache pointers for the runtime StateStore.

Approval refs record that a purpose-bound approval targeted a specific
artifact hash (the canonical decision lives in Review Events files).
Cache pointers index rebuildable artifacts by cache key; eviction must
delete the pointer together with the object or verification fails
closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from services.job_runner.state_context import StateContext
from services.job_runner.state_models import (
    ApprovalRefRow,
    CachePointerRow,
    int_column,
    text_column,
)

if TYPE_CHECKING:
    from services.contracts.primitives import ArtifactId, Identifier, Sha256


class PointerOps(StateContext):

    def record_approval_ref(
        self, *, purpose: Identifier, target_hash: Sha256, artifact_ref: ArtifactId
    ) -> ApprovalRefRow:
        row = self._connection.execute(
            "SELECT recorded_seq FROM approval_refs WHERE purpose = ?"
            " AND target_hash = ? AND artifact_ref = ?",
            (purpose, target_hash, artifact_ref),
        ).fetchone()
        if row is not None:
            return ApprovalRefRow(
                purpose=purpose,
                target_hash=target_hash,
                artifact_ref=artifact_ref,
                recorded_seq=int(row[0]),
            )
        seq = self._next_seq()
        self._connection.execute(
            "INSERT INTO approval_refs (purpose, target_hash, artifact_ref, recorded_seq)"
            " VALUES (?, ?, ?, ?)",
            (purpose, target_hash, artifact_ref, seq),
        )
        return ApprovalRefRow(
            purpose=purpose,
            target_hash=target_hash,
            artifact_ref=artifact_ref,
            recorded_seq=seq,
        )

    def get_approval_refs(self) -> tuple[ApprovalRefRow, ...]:
        rows = self._connection.execute(
            "SELECT purpose, target_hash, artifact_ref, recorded_seq"
            " FROM approval_refs ORDER BY recorded_seq"
        ).fetchall()
        return tuple(_approval_from_row(row) for row in rows)

    def put_cache_pointer(self, pointer: CachePointerRow) -> CachePointerRow:
        self._connection.execute(
            "INSERT INTO cache_pointers (cache_key, artifact_hash, producer_version)"
            " VALUES (?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET"
            " artifact_hash = excluded.artifact_hash,"
            " producer_version = excluded.producer_version",
            (pointer.cache_key, pointer.artifact_hash, pointer.producer_version),
        )
        return pointer

    def get_cache_pointer(self, cache_key: Identifier) -> CachePointerRow | None:
        row = self._connection.execute(
            "SELECT cache_key, artifact_hash, producer_version FROM cache_pointers"
            " WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        return None if row is None else _cache_pointer_from_row(row)

    def get_cache_pointers(self) -> tuple[CachePointerRow, ...]:
        rows = self._connection.execute(
            "SELECT cache_key, artifact_hash, producer_version FROM cache_pointers"
            " ORDER BY cache_key"
        ).fetchall()
        return tuple(_cache_pointer_from_row(row) for row in rows)


def _approval_from_row(row: Sequence[object]) -> ApprovalRefRow:
    return ApprovalRefRow(
        purpose=text_column(row[0]),
        target_hash=text_column(row[1]),
        artifact_ref=text_column(row[2]),
        recorded_seq=int_column(row[3]),
    )


def _cache_pointer_from_row(row: Sequence[object]) -> CachePointerRow:
    return CachePointerRow(
        cache_key=text_column(row[0]),
        artifact_hash=text_column(row[1]),
        producer_version=text_column(row[2]),
    )


__all__ = ["PointerOps"]
