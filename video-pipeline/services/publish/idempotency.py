"""Append-only upload ledger — idempotency records per channel alias (task 54).

Runtime State, not an authoritative artifact: the ledger guards against
duplicate uploads across retries and process restarts. One JSONL file per
channel alias; every append is fsynced; existing lines are never rewritten.
"""

from __future__ import annotations

import datetime
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, ValidationError

from services.contracts.primitives import StrictModel
from services.foundation_io import canonical_model_bytes

UploadStatus = Literal["started", "completed", "failed"]
LedgerClock = Callable[[], datetime.datetime]
_NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]

_CHANNEL_ALIAS_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class UploadLedgerError(RuntimeError):
    """Typed base for upload ledger failures (corrupt lines, unsafe aliases)."""


class DuplicateUploadKeyError(UploadLedgerError):
    """An idempotency key already completed — duplicate upload refused."""


class UploadLedgerRecord(StrictModel):
    """One append-only ledger event for an idempotency key."""

    idempotency_key: _NonEmptyStr
    status: UploadStatus
    video_id: _NonEmptyStr | None = None
    reason: str | None = None
    timestamp: _NonEmptyStr


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class UploadLedger:
    """Append-only JSONL ledger, one file per channel alias under ``root``.

    ``records()`` always re-reads the file from disk so a retrying process
    observes what earlier attempts actually persisted.
    """

    def __init__(
        self,
        root: Path,
        channel_alias: str,
        *,
        clock: LedgerClock = _utc_now,
    ) -> None:
        if not _CHANNEL_ALIAS_PATTERN.match(channel_alias):
            raise UploadLedgerError(
                f"channel alias is not ledger-file-safe: {channel_alias!r}",
            )
        self._path = root / f"{channel_alias}.jsonl"
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._path

    def records(self) -> list[UploadLedgerRecord]:
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return []
        parsed: list[UploadLedgerRecord] = []
        for line_number, line in enumerate(raw.splitlines(), start=1):
            if not line:
                continue
            try:
                parsed.append(UploadLedgerRecord.model_validate_json(line))
            except ValidationError as error:
                raise UploadLedgerError(
                    f"corrupt ledger line {line_number} in {self._path}",
                ) from error
        return parsed

    def lookup(self, idempotency_key: str) -> UploadLedgerRecord | None:
        for record in self.records():
            if record.idempotency_key == idempotency_key:
                return record
        return None

    def completed_video_id(self, idempotency_key: str) -> str | None:
        for record in self.records():
            if record.idempotency_key == idempotency_key and record.status == "completed":
                return record.video_id
        return None

    def start(self, idempotency_key: str) -> UploadLedgerRecord:
        records = self.records()
        if self._find_completed(records, idempotency_key) is not None:
            raise DuplicateUploadKeyError(
                f"idempotency key already completed: {idempotency_key}",
            )
        latest = self._find_latest(records, idempotency_key)
        if latest is not None and latest.status == "started":
            return latest
        return self._append(
            UploadLedgerRecord(
                idempotency_key=idempotency_key,
                status="started",
                timestamp=self._clock().isoformat(),
            ),
        )

    def complete(self, idempotency_key: str, video_id: str) -> UploadLedgerRecord:
        if self._find_completed(self.records(), idempotency_key) is not None:
            raise DuplicateUploadKeyError(
                f"idempotency key already completed: {idempotency_key}",
            )
        return self._append(
            UploadLedgerRecord(
                idempotency_key=idempotency_key,
                status="completed",
                video_id=video_id,
                timestamp=self._clock().isoformat(),
            ),
        )

    def fail(self, idempotency_key: str, *, reason: str) -> UploadLedgerRecord:
        return self._append(
            UploadLedgerRecord(
                idempotency_key=idempotency_key,
                status="failed",
                reason=reason,
                timestamp=self._clock().isoformat(),
            ),
        )

    def _find_latest(
        self,
        records: list[UploadLedgerRecord],
        idempotency_key: str,
    ) -> UploadLedgerRecord | None:
        latest: UploadLedgerRecord | None = None
        for record in records:
            if record.idempotency_key == idempotency_key:
                latest = record
        return latest

    def _find_completed(
        self,
        records: list[UploadLedgerRecord],
        idempotency_key: str,
    ) -> UploadLedgerRecord | None:
        for record in records:
            if record.idempotency_key == idempotency_key and record.status == "completed":
                return record
        return None

    def _append(self, record: UploadLedgerRecord) -> UploadLedgerRecord:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("ab") as stream:
            stream.write(canonical_model_bytes(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        return record


__all__ = [
    "DuplicateUploadKeyError",
    "LedgerClock",
    "UploadLedger",
    "UploadLedgerError",
    "UploadLedgerRecord",
    "UploadStatus",
]
