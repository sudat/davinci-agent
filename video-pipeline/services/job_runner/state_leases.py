"""SQL lease rows for the runtime StateStore.

These rows complement the flock lease authority with durable,
expiry-enforced bookkeeping. Expiry is evaluated ONLY against
caller-supplied ``now`` values (logical test clock or epoch reading);
no method reads the wall clock. Acquire after expiry steals the row,
renew/release by a non-holder or past expiry fail closed.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from services.job_runner.state_context import StateContext
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_models import LeaseRow

if TYPE_CHECKING:
    from services.contracts.primitives import Identifier


class LeaseOps(StateContext):

    def acquire_lease(
        self, *, resource: Identifier, holder: Identifier, now: int, ttl_seconds: int
    ) -> LeaseRow:
        _require_positive_ttl(ttl_seconds)
        row = self._connection.execute(
            "SELECT holder, expires_at FROM leases WHERE resource = ?", (resource,)
        ).fetchone()
        if row is not None and str(row[0]) != holder and now < int(row[1]):
            raise StateStoreError(
                "lease-held",
                f"lease {resource} is held by {row[0]} until {row[1]} (now {now})",
            )
        lease = LeaseRow(resource=resource, holder=holder, expires_at=now + ttl_seconds)
        self._connection.execute(
            "INSERT INTO leases (resource, holder, expires_at) VALUES (?, ?, ?)"
            " ON CONFLICT(resource) DO UPDATE SET holder = excluded.holder,"
            " expires_at = excluded.expires_at",
            (lease.resource, lease.holder, lease.expires_at),
        )
        return lease

    def renew_lease(
        self, *, resource: Identifier, holder: Identifier, now: int, ttl_seconds: int
    ) -> LeaseRow:
        _require_positive_ttl(ttl_seconds)
        _require_fresh_lease(self._connection, resource, holder, now)
        renewed = LeaseRow(
            resource=resource, holder=holder, expires_at=now + ttl_seconds
        )
        self._connection.execute(
            "UPDATE leases SET expires_at = ? WHERE resource = ?",
            (renewed.expires_at, resource),
        )
        return renewed

    def release_lease(self, *, resource: Identifier, holder: Identifier, now: int) -> None:
        _require_fresh_lease(self._connection, resource, holder, now)
        self._connection.execute("DELETE FROM leases WHERE resource = ?", (resource,))


def _require_positive_ttl(ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        raise StateStoreError("lease-ttl", f"ttl must be positive, got {ttl_seconds}")


def _require_fresh_lease(
    connection: sqlite3.Connection, resource: str, holder: str, now: int
) -> int:
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
    if now >= expires_at:
        raise StateStoreError(
            "lease-expired", f"lease {resource} expired at {expires_at} (now {now})"
        )
    return expires_at


__all__ = ["LeaseOps"]
