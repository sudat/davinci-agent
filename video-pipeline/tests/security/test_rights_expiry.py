"""Attack class 10: license/rights expiry blocks usage — typed, not warned.

Every expiry-shaped attack (past expiry, before effective window, wrong
territory, wrong usage, unapproved, unregistered) is attempted against
the REAL rights gate and must raise the typed ``AssetRightsError`` with
the machine reason; the registry snapshot stays sealed (immutable) after
every denial.
"""

from __future__ import annotations

import pytest

from services.presentation.asset_registry import (
    AssetEntry,
    AssetRightsError,
    AssetUsage,
    RegistrySnapshot,
    check_rights,
)

JOB_DATE = "2026-08-20"


def _entry(**overrides: object) -> AssetEntry:
    fields: dict[str, object] = {
        "asset_id": "asset:tone-99",
        "sha256": "9" * 64,
        "path": "assets/tone-99.wav",
        "usage": "tone",
        "territories": ("WORLDWIDE",),
        "effective_date": "2020-01-01",
        "expiry_date": "2026-01-01",
        "license_evidence_ref": "lic/tone-99.pdf",
        "attribution": None,
        "content_id_notes": None,
        "approved": True,
    }
    fields.update(overrides)
    return AssetEntry.model_validate(fields)


def _registry(**overrides: object) -> RegistrySnapshot:
    draft = RegistrySnapshot(
        entries=(_entry(**overrides),), registry_snapshot_sha256="0" * 64
    )
    return draft.model_copy(update={"registry_snapshot_sha256": draft.content_hash()})


def attempt(
    registry: RegistrySnapshot,
    *,
    at_job: str = JOB_DATE,
    territory: str = "WORLDWIDE",
    usage: AssetUsage = "tone",
    asset_id: str = "asset:tone-99",
) -> AssetRightsError | None:
    try:
        check_rights(
            registry, asset_id, usage=usage, territory=territory, at_job=at_job
        )
    except AssetRightsError as error:
        return error
    return None


def test_10_expired_license_blocks() -> None:
    registry = _registry()
    error = attempt(registry)
    assert error is not None
    assert error.reason == "expired"
    assert registry.verify_hash() is True


def test_11_before_effective_window_blocks() -> None:
    registry = _registry(effective_date="2030-01-01", expiry_date=None)
    error = attempt(registry)
    assert error is not None
    assert error.reason == "expired"


def test_12_wrong_territory_blocks() -> None:
    error = attempt(_registry(territories=("JP",)), territory="US")
    assert error is not None
    assert error.reason == "territory"


def test_13_wrong_usage_blocks() -> None:
    error = attempt(_registry(), usage="se")
    assert error is not None
    assert error.reason == "wrong_use"


def test_14_unapproved_asset_blocks() -> None:
    registry = _registry(approved=False)
    error = attempt(registry)
    assert error is not None
    assert error.reason == "unapproved"


def test_15_unregistered_asset_blocks() -> None:
    error = attempt(_registry(), asset_id="asset:ghost")
    assert error is not None
    assert error.reason == "missing"


def test_20_valid_window_passes_and_registry_stays_sealed() -> None:
    registry = _registry(expiry_date="2099-01-01")
    entry = check_rights(
        registry, "asset:tone-99", usage="tone", territory="WORLDWIDE", at_job=JOB_DATE
    )
    assert entry.asset_id == "asset:tone-99"
    assert registry.verify_hash() is True


def test_21_denials_never_mutate_the_sealed_snapshot() -> None:
    registry = _registry()
    sealed = registry.registry_snapshot_sha256
    for _ in range(3):
        assert attempt(registry) is not None
    assert registry.registry_snapshot_sha256 == sealed
    assert registry.verify_hash() is True


def test_22_inverted_window_is_rejected_at_model_level() -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011 (typed pydantic window error)
        _registry(effective_date="2026-01-01", expiry_date="2020-01-01")
