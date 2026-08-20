"""Append-only deletion audit: one canonical JSONL record per decision.

The audit file lives at the jobs root (never inside a job directory),
so no GC pass can ever sweep its own evidence; existing lines are never
rewritten, and every append is fsynced like the other ledgers.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import canonical_model_bytes
from services.retention.errors import RetentionError
from services.retention.records import AuditRecord

AUDIT_FILE_NAME = "gc-audit.jsonl"


class DeletionAudit:
    """Append-only audit ledger for one jobs root."""

    def __init__(self, jobs_root: Path) -> None:
        self._root = jobs_root
        self._path = jobs_root / AUDIT_FILE_NAME

    @property
    def path(self) -> Path:
        return self._path

    def append(self, records: tuple[AuditRecord, ...]) -> None:
        if not records:
            return
        payload = b"".join(canonical_model_bytes(record) + b"\n" for record in records)
        self._root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def records(self) -> tuple[AuditRecord, ...]:
        """Parse and validate the whole ledger; any tampering is a typed error."""

        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return ()
        parsed: list[AuditRecord] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                parsed.append(AuditRecord.model_validate_json(line))
            except ValidationError as error:
                raise RetentionError(
                    "audit-invalid", f"audit line rejected (tamper/malformed): {error}"
                ) from error
        return tuple(parsed)


__all__ = ["AUDIT_FILE_NAME", "DeletionAudit"]
