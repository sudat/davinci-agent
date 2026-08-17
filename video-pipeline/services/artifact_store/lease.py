"""Minimal single-host lease authority (flock + expiry timestamp).

Spike-minimal on purpose: acquire and commit-check only. The full state
machine (renewal, fencing tokens, queueing) is Todo 10."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from pydantic import Field

from services.contracts.primitives import Identifier, StrictModel
from services.foundation_io import canonical_model_bytes


class LeaseError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _lease_path(lease_root: Path, name: str) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
        raise LeaseError("lease-name", f"refusing lease name: {name!r}")
    return lease_root / f"{name}.json"


class LeaseRecord(StrictModel):
    holder: Identifier
    acquired_at_unix: int = Field(ge=0, strict=True)
    expires_at_unix: int = Field(ge=0, strict=True)


def _read_record(descriptor: int) -> LeaseRecord | None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = os.read(descriptor, os.fstat(descriptor).st_size)
    if not raw:
        return None
    return LeaseRecord.model_validate_json(raw)


class LeaseAuthority:
    def __init__(self, lease_root: Path) -> None:
        self._root = lease_root
        lease_root.mkdir(parents=True, exist_ok=True)

    def acquire(
        self,
        name: Identifier,
        holder: Identifier,
        *,
        now_unix: int,
        ttl_seconds: int,
    ) -> LeaseRecord:
        path = _lease_path(self._root, name)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            record = _read_record(descriptor)
            if (
                record is not None
                and record.holder != holder
                and now_unix < record.expires_at_unix
            ):
                raise LeaseError("lease-held", f"lease {name} is held by {record.holder}")
            new_record = LeaseRecord(
                holder=holder,
                acquired_at_unix=now_unix,
                expires_at_unix=now_unix + ttl_seconds,
            )
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, canonical_model_bytes(new_record))
            os.fsync(descriptor)
            return new_record
        finally:
            os.close(descriptor)

    def commit(self, name: Identifier, holder: Identifier, *, now_unix: int) -> LeaseRecord:
        path = _lease_path(self._root, name)
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError as error:
            raise LeaseError("lease-missing", f"no lease recorded for {name}") from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            record = _read_record(descriptor)
            if record is None:
                raise LeaseError("lease-missing", f"lease file for {name} is empty")
            if record.holder != holder:
                raise LeaseError("not-holder", f"lease {name} is held by {record.holder}")
            if now_unix >= record.expires_at_unix:
                raise LeaseError(
                    "lease-expired",
                    f"lease {name} expired at {record.expires_at_unix}",
                )
            return record
        finally:
            os.close(descriptor)


__all__ = [
    "LeaseAuthority",
    "LeaseError",
    "LeaseRecord",
]
