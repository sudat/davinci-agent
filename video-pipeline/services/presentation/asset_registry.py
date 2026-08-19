"""Presentation Asset Registry (PRD 26): rights-safe versioned assets.

An asset is fixed by content hash, never by path alone: every entry carries
usage, territories, an effective/expiry window (ISO dates, no floats),
license evidence, attribution, Content ID notes, and the approved state. The
registry snapshot is immutable and hash-bound; the rights gate refuses
missing / wrong-use / uncovered-territory / unapproved / expired / changed
bytes with typed blocking errors. The importer registers the twelve frozen
Todo-56 assets at exactly their manifest hashes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.contracts.serialization import GENESIS_SHA256, canonical_json_bytes
from services.fixtures.manifest_phase3 import PHASE_3_FIXTURE_IDS, Phase3FixtureManifest
from services.presentation.models import Sequence  # noqa: TC001 (pydantic runtime)

type AssetUsage = Literal["intro", "logo", "outro", "overlay", "se", "tone"]
type IsoDate = Annotated[
    str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$", strict=True)
]
type TerritoryCode = Annotated[
    str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9-]*$", strict=True)
]
type RightsReason = Literal[
    "missing", "wrong_use", "territory", "unapproved", "expired", "changed_bytes"
]

PHASE_3_ASSET_EFFECTIVE_DATE: IsoDate = "1970-01-01"


class AssetRightsError(ValueError):
    """Typed blocking rights failure; ``reason`` carries the machine cause."""

    def __init__(self, reason: RightsReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class AssetEntry(StrictModel):
    asset_id: Identifier
    sha256: Sha256
    path: str = Field(min_length=1)
    usage: AssetUsage
    territories: Sequence[TerritoryCode] = Field(min_length=1)
    effective_date: IsoDate
    expiry_date: IsoDate | None
    license_evidence_ref: str = Field(min_length=1)
    attribution: str | None
    content_id_notes: str | None
    approved: bool

    @model_validator(mode="after")
    def require_sane_window(self) -> AssetEntry:
        if self.expiry_date is not None and self.expiry_date < self.effective_date:
            raise PydanticCustomError(
                "window_inverted", "expiry_date must not precede effective_date"
            )
        return self


class RegistrySnapshot(StrictModel):
    """Immutable registry snapshot; entries sorted by asset_id, hash-sealed."""

    schema_version: Literal["asset-registry-snapshot-v1"] = "asset-registry-snapshot-v1"
    entries: tuple[AssetEntry, ...] = Field(min_length=1)
    registry_snapshot_sha256: Sha256

    @model_validator(mode="after")
    def require_sorted_unique_entries(self) -> RegistrySnapshot:
        asset_ids = [entry.asset_id for entry in self.entries]
        if len(set(asset_ids)) != len(asset_ids):
            raise PydanticCustomError("duplicate_asset", "asset ids must be unique")
        if asset_ids != sorted(asset_ids):
            raise PydanticCustomError("unsorted_assets", "entries must be sorted")
        return self

    def entry_for(self, asset_id: str) -> AssetEntry | None:
        for entry in self.entries:
            if entry.asset_id == asset_id:
                return entry
        return None

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    def content_hash(self) -> Sha256:
        zeroed = self.model_copy(
            update={"registry_snapshot_sha256": GENESIS_SHA256}
        )
        return hashlib.sha256(canonical_json_bytes(zeroed)).hexdigest()

    def verify_hash(self) -> bool:
        return self.registry_snapshot_sha256 == self.content_hash()


def register_assets(*entries: AssetEntry) -> RegistrySnapshot:
    """Register entries into a fresh sealed snapshot (single writer per call)."""

    draft = RegistrySnapshot(
        entries=tuple(sorted(entries, key=lambda entry: entry.asset_id)),
        registry_snapshot_sha256=GENESIS_SHA256,
    )
    return draft.model_copy(update={"registry_snapshot_sha256": draft.content_hash()})


def check_rights(  # noqa: PLR0913 (gate signature fixed by the Todo-55 contract)
    registry: RegistrySnapshot,
    asset_id: str,
    *,
    usage: AssetUsage,
    territory: TerritoryCode,
    at_job: IsoDate,
    observed_sha256: Sha256 | None = None,
) -> AssetEntry:
    """Rights gate: every refusal is a typed blocking error, never a warning."""

    entry = registry.entry_for(asset_id)
    if entry is None:
        raise AssetRightsError("missing", f"asset is not registered: {asset_id}")
    if entry.usage != usage:
        raise AssetRightsError(
            "wrong_use", f"asset {asset_id} is licensed for {entry.usage}, not {usage}"
        )
    if territory not in entry.territories:
        raise AssetRightsError(
            "territory", f"asset {asset_id} does not cover territory {territory}"
        )
    if not entry.approved:
        raise AssetRightsError("unapproved", f"asset {asset_id} is not approved")
    expired = at_job < entry.effective_date or (
        entry.expiry_date is not None and at_job > entry.expiry_date
    )
    if expired:
        raise AssetRightsError(
            "expired", f"asset {asset_id} rights do not cover job date {at_job}"
        )
    if observed_sha256 is not None and observed_sha256 != entry.sha256:
        raise AssetRightsError(
            "changed_bytes", f"asset {asset_id} bytes drifted from the registered hash"
        )
    return entry


def _fixture_entries(
    manifest: Phase3FixtureManifest, manifest_root: Path, manifest_sha: str
) -> tuple[AssetEntry, ...]:
    return tuple(
        AssetEntry(
            asset_id=f"{manifest.fixture_id}:{asset.kind}",
            sha256=asset.sha256,
            path=(manifest_root / asset.path).as_posix(),
            usage=asset.kind,
            territories=("WORLDWIDE",),
            effective_date=PHASE_3_ASSET_EFFECTIVE_DATE,
            expiry_date=None,
            license_evidence_ref=(
                f"phase3-fixture:{manifest.fixture_id}:{asset.kind}:{manifest_sha}"
            ),
            attribution=None,
            content_id_notes=None,
            approved=True,
        )
        for asset in manifest.presentation.assets
    )


def registry_from_phase3_manifests(
    manifest_root: Path,
    fixture_ids: Sequence[str] = PHASE_3_FIXTURE_IDS,
) -> RegistrySnapshot:
    """Register the frozen Todo-56 brand assets at their manifest hashes."""

    entries: list[AssetEntry] = []
    for fixture_id in fixture_ids:
        manifest_path = manifest_root / f"{fixture_id}.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = Phase3FixtureManifest.model_validate_json(manifest_bytes)
        entries.extend(
            _fixture_entries(
                manifest, manifest_root, hashlib.sha256(manifest_bytes).hexdigest()
            )
        )
    return register_assets(*entries)


__all__ = [
    "PHASE_3_ASSET_EFFECTIVE_DATE",
    "AssetEntry",
    "AssetRightsError",
    "AssetUsage",
    "IsoDate",
    "RegistrySnapshot",
    "RightsReason",
    "TerritoryCode",
    "check_rights",
    "register_assets",
    "registry_from_phase3_manifests",
]

