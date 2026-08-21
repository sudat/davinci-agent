"""SQL lease rows for the runtime StateStore.

These rows complement the flock lease authority with durable,
expiry-enforced bookkeeping. Expiry is evaluated ONLY against
caller-supplied ``now`` values (logical test clock or epoch reading);
no method reads the wall clock. Holders are INVOCATION-UNIQUE tokens
(e.g. ``f"{role}:{uuid4}"``): acquisition may only take an EXPIRED row —
a second process claiming the same role with a fresh token is refused
while the lease lives, and the owner refreshes exclusively through
``renew_lease``. Every mutation is ONE atomic conditional statement under
``BEGIN IMMEDIATE`` (acquire = conditional upsert that only overwrites
an expired row; renew and release re-check holder and expiry in the
statement's WHERE clause), and refusal is decided by the affected-row
count — so two connections can never both observe success, and a reader
that raced with a fresh committer is refused instead of blindly
overwriting.
"""

from __future__ import annotations

import sqlite3
import types
from typing import TYPE_CHECKING, Final

from services.job_runner.state_context import StateContext
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_models import LeaseRow

if TYPE_CHECKING:
    from services.contracts.primitives import Identifier

_ACQUIRE_SQL: Final = (
    "INSERT INTO leases (resource, holder, expires_at) VALUES (?, ?, ?)"
    " ON CONFLICT(resource) DO UPDATE SET holder = excluded.holder,"
    " expires_at = excluded.expires_at"
    " WHERE leases.expires_at <= ?"
)
_RENEW_SQL: Final = (
    "UPDATE leases SET expires_at = ? WHERE resource = ? AND holder = ?"
    " AND expires_at > ?"
)
_RELEASE_SQL: Final = "DELETE FROM leases WHERE resource = ? AND holder = ? AND expires_at > ?"


class LeaseOps(StateContext):

    def acquire_lease(
        self, *, resource: Identifier, holder: Identifier, now: int, ttl_seconds: int
    ) -> LeaseRow:
        """Take the lease ONLY if free or expired (never from a fresh holder)."""
        _require_positive_ttl(ttl_seconds)
        lease = LeaseRow(resource=resource, holder=holder, expires_at=now + ttl_seconds)
        with _immediate(self._connection):
            cursor = self._connection.execute(
                _ACQUIRE_SQL, (resource, holder, lease.expires_at, now)
            )
            if cursor.rowcount != 1:
                raise StateStoreError(
                    "lease-held",
                    f"lease {resource} is held by a fresh holder (now {now}); "
                    "the owner renews via renew_lease, others wait for expiry",
                )
        return lease

    def renew_lease(
        self, *, resource: Identifier, holder: Identifier, now: int, ttl_seconds: int
    ) -> LeaseRow:
        _require_positive_ttl(ttl_seconds)
        renewed = LeaseRow(
            resource=resource, holder=holder, expires_at=now + ttl_seconds
        )
        with _immediate(self._connection):
            cursor = self._connection.execute(
                _RENEW_SQL, (renewed.expires_at, resource, holder, now)
            )
            if cursor.rowcount != 1:
                _raise_stale_lease(self._connection, resource, holder, now)
        return renewed

    def release_lease(
        self, *, resource: Identifier, holder: Identifier, now: int
    ) -> None:
        with _immediate(self._connection):
            cursor = self._connection.execute(
                _RELEASE_SQL, (resource, holder, now)
            )
            if cursor.rowcount != 1:
                _raise_stale_lease(self._connection, resource, holder, now)


class _Immediate:
    """``BEGIN IMMEDIATE`` … ``COMMIT`` context (rollback on error)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        if exc_type is None:
            self._connection.execute("COMMIT")
        else:
            self._connection.execute("ROLLBACK")


def _immediate(connection: sqlite3.Connection) -> _Immediate:
    return _Immediate(connection)


def _raise_stale_lease(
    connection: sqlite3.Connection, resource: str, holder: str, now: int
) -> None:
    row = connection.execute(
        "SELECT holder, expires_at FROM leases WHERE resource = ?", (resource,)
    ).fetchone()
    if row is None:
        raise StateStoreError("lease-missing", f"no lease row for {resource}")
    current_holder, expires_at = str(row[0]), int(row[1])
    if current_holder != holder:
        raise StateStoreError(
            "not-holder", f"lease {resource} is held by {current_holder}"
        )
    raise StateStoreError(
        "lease-expired", f"lease {resource} expired at {expires_at} (now {now})"
    )


def _require_positive_ttl(ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        raise StateStoreError("lease-ttl", f"ttl must be positive, got {ttl_seconds}")


__all__ = ["LeaseOps"]
