"""Job-start presentation snapshot: freeze and stale-state gating.

The snapshot freezes the resolved profile hash, the registry snapshot hash,
and the OBSERVED asset bytes at Job start. Any later drift — profile file,
registry contents, or asset bytes — fails verification with a typed blocking
error (:class:`StaleSnapshotError`); nothing re-reads mutable state silently.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.contracts.serialization import GENESIS_SHA256, canonical_json_bytes
from services.foundation_io import sha256_file
from services.presentation.asset_registry import (
    AssetUsage,
    IsoDate,
    RegistrySnapshot,
    TerritoryCode,
    check_rights,
)

if TYPE_CHECKING:
    from services.presentation.models import ResolvedPresentationProfile


class AssetBytesObservation(StrictModel):
    asset_id: Identifier
    sha256: Sha256


class JobPresentationSnapshot(StrictModel):
    """Job-start freeze of profile + registry + observed asset bytes."""

    schema_version: Literal["job-presentation-snapshot-v1"] = (
        "job-presentation-snapshot-v1"
    )
    job_date: IsoDate
    job_territory: TerritoryCode
    profile_snapshot_sha256: Sha256
    registry_snapshot_sha256: Sha256
    asset_bytes: tuple[AssetBytesObservation, ...] = Field(min_length=1)
    snapshot_sha256: Sha256

    @model_validator(mode="after")
    def require_sorted_unique_observations(self) -> JobPresentationSnapshot:
        asset_ids = [observation.asset_id for observation in self.asset_bytes]
        if len(set(asset_ids)) != len(asset_ids):
            raise PydanticCustomError("duplicate_observation", "one observation per asset")
        if asset_ids != sorted(asset_ids):
            raise PydanticCustomError("unsorted_observations", "observations must be sorted")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    def content_hash(self) -> Sha256:
        zeroed = self.model_copy(update={"snapshot_sha256": GENESIS_SHA256})
        return hashlib.sha256(canonical_json_bytes(zeroed)).hexdigest()

    def verify_hash(self) -> bool:
        return self.snapshot_sha256 == self.content_hash()


class StaleSnapshotError(RuntimeError):
    """The job-start presentation snapshot no longer matches observable state."""


def _asset_path(assets_root: Path, entry_path: str) -> Path:
    path = Path(entry_path)
    return path if path.is_absolute() else assets_root / path


def freeze_job_presentation(
    resolved_profile: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    *,
    job_date: IsoDate,
    job_territory: TerritoryCode,
    assets_root: Path,
) -> JobPresentationSnapshot:
    """Gate rights and freeze observed asset bytes at Job start."""

    observations: list[AssetBytesObservation] = []
    for binding in resolved_profile.asset_bindings:
        usage: AssetUsage = binding.kind
        entry = check_rights(
            registry, binding.asset_id, usage=usage, territory=job_territory, at_job=job_date
        )
        observed = sha256_file(_asset_path(assets_root, entry.path))
        check_rights(
            registry,
            binding.asset_id,
            usage=usage,
            territory=job_territory,
            at_job=job_date,
            observed_sha256=observed,
        )
        observations.append(
            AssetBytesObservation(asset_id=binding.asset_id, sha256=observed)
        )
    draft = JobPresentationSnapshot(
        job_date=job_date,
        job_territory=job_territory,
        profile_snapshot_sha256=resolved_profile.content_hash(),
        registry_snapshot_sha256=registry.content_hash(),
        asset_bytes=tuple(sorted(observations, key=lambda item: item.asset_id)),
        snapshot_sha256=GENESIS_SHA256,
    )
    return draft.model_copy(update={"snapshot_sha256": draft.content_hash()})


def verify_job_presentation(
    snapshot: JobPresentationSnapshot,
    resolved_profile: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    assets_root: Path,
) -> None:
    """Block on any post-snapshot drift: profile, registry, or asset bytes."""

    if resolved_profile.content_hash() != snapshot.profile_snapshot_sha256:
        raise StaleSnapshotError(
            "presentation profile changed after the job-start snapshot"
        )
    if registry.content_hash() != snapshot.registry_snapshot_sha256:
        raise StaleSnapshotError("asset registry changed after the job-start snapshot")
    recorded = {
        observation.asset_id: observation.sha256 for observation in snapshot.asset_bytes
    }
    for binding in resolved_profile.asset_bindings:
        entry = registry.entry_for(binding.asset_id)
        if entry is None:
            raise StaleSnapshotError(
                f"bound asset left the registry after the job-start snapshot: "
                f"{binding.asset_id}"
            )
        current = sha256_file(_asset_path(assets_root, entry.path))
        if current != recorded[binding.asset_id] or current != entry.sha256:
            raise StaleSnapshotError(
                f"asset bytes changed after the job-start snapshot: {binding.asset_id}"
            )


__all__ = [
    "AssetBytesObservation",
    "JobPresentationSnapshot",
    "StaleSnapshotError",
    "freeze_job_presentation",
    "verify_job_presentation",
]
