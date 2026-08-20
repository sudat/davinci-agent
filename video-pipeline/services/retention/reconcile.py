"""Hash/registry reconciliation: deletion gating against registry truth."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from services.retention.models import RetentionRegistry

Verdict = Literal["verified", "unregistered", "hash-mismatch"]


def reconcile(
    registry: RetentionRegistry | None, job_relative_path: str, disk_sha256: str
) -> Verdict:
    """Decide whether the registry vouches for exactly these disk bytes.

    A missing registry, a missing entry, or any disagreement between the
    registered hash and the disk hash blocks deletion (conservative).
    """

    if registry is None:
        return "unregistered"
    registered = registry.entries.get(job_relative_path)
    if registered is None:
        return "unregistered"
    if registered.sha256 != disk_sha256:
        return "hash-mismatch"
    return "verified"


__all__ = ["Verdict", "reconcile"]
