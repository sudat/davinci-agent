"""Append-only keyed hash-chained operation-record store with supersession.

Layout: one JSONL file, one canonical ``ChainedOperationRecord`` per
line, sealed under the store-local HMAC key (``chain_key.py``):
``record_hash`` is HMAC-SHA256(chain_key, record bytes with the hash
zeroed) plus ``previous_record_hash``, so mutation, reordering, interior
deletion, AND offline-forged self-consistent chains are all detected by
``verify_chain``. Supersession: appending a record with the same
(purpose, target_hash) as the current latest marks that record superseded
via the new record's ``superseded_record_id``; superseded records are
retained for audit and never returned by ``latest_records``.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from services.approvals.chain_key import ChainKeyError, load_chain_key
from services.approvals.models import (
    GENESIS_RECORD_HASH,
    ChainedOperationRecord,
    OperationDraft,
    record_id_for_seq,
)
from services.approvals.verify import validate_supersession_chain
from services.contracts.serialization import canonical_json_bytes


class OperationRecordError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class OperationRecordStore:
    def __init__(self, records_path: Path) -> None:
        self._path = records_path
        records_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def records_path(self) -> Path:
        return self._path

    def all_records(self) -> tuple[ChainedOperationRecord, ...]:
        return _read_records(self._path)

    def latest_records(self) -> dict[tuple[str, str], ChainedOperationRecord]:
        records = self.all_records()
        superseded_ids = {record.superseded_record_id for record in records}
        latest: dict[tuple[str, str], ChainedOperationRecord] = {}
        for record in records:
            if record.record_id not in superseded_ids:
                latest[record.supersession_key] = record
        return latest

    def append(self, draft: OperationDraft) -> ChainedOperationRecord:
        records = self.all_records()
        chain_key = self._chain_key(create=True)
        try:
            validate_supersession_chain(records, chain_key=chain_key)
        except ValueError as error:
            raise OperationRecordError("chain-invalid", str(error)) from error
        seq = 1 if not records else records[-1].timestamp_seq + 1
        previous = (
            GENESIS_RECORD_HASH if not records else records[-1].record_hash
        )
        superseded_id: str | None = None
        current = self.latest_records().get(draft.supersession_key)
        if current is not None:
            superseded_id = current.record_id
        unsealed = ChainedOperationRecord(
            **draft.model_dump(),
            record_id=record_id_for_seq(seq),
            timestamp_seq=seq,
            superseded_record_id=superseded_id,
            previous_record_hash=previous,
            record_hash=GENESIS_RECORD_HASH,
        )
        record = unsealed.model_copy(
            update={"record_hash": unsealed.recomputed_record_hash(chain_key=chain_key)}
        )
        descriptor = os.open(
            self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            os.write(descriptor, canonical_json_bytes(record) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return record

    def verify_chain(self) -> tuple[ChainedOperationRecord, ...]:
        records = _read_records(self._path)
        if not records:
            return records
        chain_key = self._chain_key(create=False)
        try:
            validate_supersession_chain(records, chain_key=chain_key)
        except ValueError as error:
            raise OperationRecordError("chain-invalid", str(error)) from error
        return records

    def _chain_key(self, *, create: bool) -> bytes:
        try:
            return load_chain_key(self._path, create=create)
        except ChainKeyError as error:
            raise OperationRecordError(error.code, error.detail) from error


def _read_records(path: Path) -> tuple[ChainedOperationRecord, ...]:
    if not path.exists():
        return ()
    records: list[ChainedOperationRecord] = []
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            records.append(ChainedOperationRecord.model_validate_json(line))
        except ValidationError as error:
            raise OperationRecordError(
                "record-line-invalid",
                f"operation-record line rejected (tamper/malformed): {error}",
            ) from error
    return tuple(records)


__all__ = [
    "GENESIS_RECORD_HASH",
    "OperationRecordError",
    "OperationRecordStore",
    "record_id_for_seq",
]
