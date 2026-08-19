"""Todo-55 acceptance: presentation profiles, asset registry, rights gates.

Layer precedence and NARROWING-ONLY resolution mirror the Todo-12 config
resolver; the registry registers the twelve frozen Todo-56 assets at their
manifest hashes; every rights failure mode is a typed blocking error. Guards:
no profile self-update path may exist and no Internet fetch code may exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.fixtures.manifest_phase3 import PHASE_3_FIXTURE_IDS, Phase3FixtureManifest
from services.presentation.asset_registry import (
    AssetEntry,
    AssetRightsError,
    RegistrySnapshot,
    check_rights,
    register_assets,
    registry_from_phase3_manifests,
)
from services.presentation.models import (
    ApprovedPresentationOverride,
    ChannelPresentationProfile,
    EpisodePresentationProfile,
    GenrePresentationProfile,
    SystemPresentationProfile,
)
from services.presentation.profiles import (
    ProfileResolutionError,
    resolve_presentation_profile,
)

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-3")
JOB_DATE = "2026-01-01"
JOB_TERRITORY = "WORLDWIDE"


def _load(fixture_id: str) -> Phase3FixtureManifest:
    return Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
    )


def _style_of(manifest: Phase3FixtureManifest) -> dict[str, object]:
    return dict(manifest.presentation.subtitle_style.model_dump())


def _color_of(manifest: Phase3FixtureManifest) -> dict[str, object]:
    return dict(manifest.presentation.color_profile.model_dump())


def _audio_of(manifest: Phase3FixtureManifest) -> dict[str, object]:
    return dict(manifest.presentation.audio.model_dump())


def _placement_of(manifest: Phase3FixtureManifest) -> dict[str, object]:
    return dict(manifest.presentation.placement.model_dump())


def _bindings_of(manifest: Phase3FixtureManifest) -> list[dict[str, object]]:
    return [
        {"kind": asset.kind, "asset_id": f"{manifest.fixture_id}:{asset.kind}"}
        for asset in manifest.presentation.assets
    ]


def _catalog() -> tuple[str, ...]:
    ids = [
        f"{fixture_id}:{asset.kind}"
        for fixture_id in PHASE_3_FIXTURE_IDS
        for asset in _load(fixture_id).presentation.assets
    ]
    return tuple(sorted(ids))


@pytest.fixture(scope="module")
def brand_a() -> Phase3FixtureManifest:
    return _load("p3-brand-a")


@pytest.fixture(scope="module")
def brand_b() -> Phase3FixtureManifest:
    return _load("p3-brand-b")


@pytest.fixture(scope="module")
def registry() -> RegistrySnapshot:
    return registry_from_phase3_manifests(MANIFEST_DIR)


def _system(brand: Phase3FixtureManifest) -> SystemPresentationProfile:
    return SystemPresentationProfile.model_validate(
        {
            "asset_catalog": list(_catalog()),
            "subtitle_style": _style_of(brand),
            "color_profile": _color_of(brand),
            "audio": _audio_of(brand),
            "placement": _placement_of(brand),
            "asset_bindings": _bindings_of(brand),
        }
    )


def _episode() -> EpisodePresentationProfile:
    return EpisodePresentationProfile(episode_id="episode-p3")


def _channel_brand_b(brand: Phase3FixtureManifest) -> ChannelPresentationProfile:
    return ChannelPresentationProfile.model_validate(
        {
            "channel_id": "channel-brand-b",
            "subtitle_style": _style_of(brand),
            "color_profile": _color_of(brand),
            "audio": _audio_of(brand),
            "placement": _placement_of(brand),
            "asset_bindings": _bindings_of(brand),
        }
    )


def test_layer_precedence_scalars_last_win(
    brand_a: Phase3FixtureManifest, registry: RegistrySnapshot
) -> None:
    base = _audio_of(brand_a)
    resolved = resolve_presentation_profile(
        _system(brand_a),
        episode=EpisodePresentationProfile.model_validate(
            {"episode_id": "episode-p3", "audio": {**base, "tone_hz": 340}}
        ),
        genre=GenrePresentationProfile.model_validate(
            {"genre_id": "genre-g", "audio": {**base, "tone_hz": 300}}
        ),
        channel=ChannelPresentationProfile.model_validate(
            {"channel_id": "channel-c", "audio": {**base, "tone_hz": 330}}
        ),
        override=ApprovedPresentationOverride.model_validate(
            {
                "override_id": "override-1",
                "approved_by": "operator",
                "reason": "loudness fix",
                "audio": {**base, "tone_hz": 350},
            }
        ),
        registry=registry,
    )
    assert resolved.audio.tone_hz == 350
    assert tuple(resolved.layers_applied) == (
        "genre-g", "channel-c", "episode-p3", "override-1",
    )

    without_override = resolve_presentation_profile(
        _system(brand_a),
        episode=EpisodePresentationProfile.model_validate(
            {"episode_id": "episode-p3", "audio": {**base, "tone_hz": 340}}
        ),
        genre=GenrePresentationProfile.model_validate(
            {"genre_id": "genre-g", "audio": {**base, "tone_hz": 300}}
        ),
        channel=ChannelPresentationProfile.model_validate(
            {"channel_id": "channel-c", "audio": {**base, "tone_hz": 330}}
        ),
        registry=registry,
    )
    assert without_override.audio.tone_hz == 340


def test_brand_b_channel_resolves_over_brand_a_system(
    brand_a: Phase3FixtureManifest, brand_b: Phase3FixtureManifest, registry: RegistrySnapshot
) -> None:
    resolved = resolve_presentation_profile(
        _system(brand_a),
        episode=_episode(),
        channel=_channel_brand_b(brand_b),
        registry=registry,
    )

    assert resolved.audio.tone_hz == brand_b.presentation.audio.tone_hz
    assert resolved.subtitle_style.primary_color_hex == (
        brand_b.presentation.subtitle_style.primary_color_hex
    )
    binding_ids = {binding.kind: binding.asset_id for binding in resolved.asset_bindings}
    assert binding_ids["intro"] == "p3-brand-b:intro"
    assert resolved.verify_hash() is True


def test_unregistered_asset_binding_is_refused(
    brand_a: Phase3FixtureManifest, registry: RegistrySnapshot
) -> None:
    bindings = _bindings_of(brand_a)
    bindings[0] = {"kind": "intro", "asset_id": "unknown-brand:intro"}

    with pytest.raises(ProfileResolutionError, match="unregistered"):
        resolve_presentation_profile(
            SystemPresentationProfile.model_validate(
                {
                    "asset_catalog": list(_catalog()),
                    "subtitle_style": _style_of(brand_a),
                    "color_profile": _color_of(brand_a),
                    "audio": _audio_of(brand_a),
                    "placement": _placement_of(brand_a),
                    "asset_bindings": bindings,
                }
            ),
            episode=_episode(),
            registry=registry,
        )


def test_catalog_widening_is_refused(
    brand_a: Phase3FixtureManifest, registry: RegistrySnapshot
) -> None:
    with pytest.raises(ProfileResolutionError, match="narrow"):
        resolve_presentation_profile(
            _system(brand_a),
            episode=_episode(),
            channel=ChannelPresentationProfile.model_validate(
                {
                    "channel_id": "channel-wide",
                    "asset_catalog": [*_catalog(), "extra-brand:intro"],
                }
            ),
            registry=registry,
        )


def test_catalog_narrowing_is_allowed(
    brand_a: Phase3FixtureManifest, brand_b: Phase3FixtureManifest, registry: RegistrySnapshot
) -> None:
    narrowed = tuple(
        asset_id for asset_id in _catalog() if asset_id.startswith("p3-brand-b:")
    )
    resolved = resolve_presentation_profile(
        _system(brand_a),
        episode=_episode(),
        channel=ChannelPresentationProfile.model_validate(
            {
                "channel_id": "channel-b-only",
                "asset_catalog": list(narrowed),
                "asset_bindings": _bindings_of(brand_b),
            }
        ),
        registry=registry,
    )
    assert set(resolved.asset_catalog) == set(narrowed)
    assert all(
        binding.asset_id.startswith("p3-brand-b:") for binding in resolved.asset_bindings
    )


def test_resolved_profile_snapshot_is_immutable_and_hash_bound(
    brand_a: Phase3FixtureManifest, registry: RegistrySnapshot
) -> None:
    resolved = resolve_presentation_profile(
        _system(brand_a), episode=_episode(), registry=registry
    )

    assert resolved.verify_hash() is True
    with pytest.raises(ValidationError):
        resolved.audio.tone_hz = 999  # type: ignore[misc]

    tampered = resolved.model_copy(
        update={"audio": resolved.audio.model_copy(update={"tone_hz": 999})}
    )
    assert tampered.verify_hash() is False


def test_registry_registers_twelve_frozen_assets_at_manifest_hashes(
    registry: RegistrySnapshot, brand_a: Phase3FixtureManifest
) -> None:
    assert registry.verify_hash() is True
    entries = {entry.asset_id: entry for entry in registry.entries}
    assert len(entries) == 12
    for asset in brand_a.presentation.assets:
        entry = entries[f"p3-brand-a:{asset.kind}"]
        assert entry.sha256 == asset.sha256
        assert entry.usage == asset.kind
        assert entry.approved is True
        assert entry.expiry_date is None
        assert (MANIFEST_DIR / asset.path).is_file()


def test_approved_assets_pass_rights_gate(registry: RegistrySnapshot) -> None:
    for entry in registry.entries:
        checked = check_rights(
            registry,
            entry.asset_id,
            usage=entry.usage,
            territory=JOB_TERRITORY,
            at_job=JOB_DATE,
        )
        assert checked.asset_id == entry.asset_id


def test_missing_asset_blocks(registry: RegistrySnapshot) -> None:
    with pytest.raises(AssetRightsError) as failure:
        check_rights(
            registry,
            "p3-brand-z:intro",
            usage="intro",
            territory=JOB_TERRITORY,
            at_job=JOB_DATE,
        )
    assert failure.value.reason == "missing"


def test_wrong_use_blocks(registry: RegistrySnapshot) -> None:
    with pytest.raises(AssetRightsError) as failure:
        check_rights(
            registry,
            "p3-brand-a:intro",
            usage="outro",
            territory=JOB_TERRITORY,
            at_job=JOB_DATE,
        )
    assert failure.value.reason == "wrong_use"


def test_territory_not_covered_blocks(registry: RegistrySnapshot) -> None:
    with pytest.raises(AssetRightsError) as failure:
        check_rights(
            registry,
            "p3-brand-a:intro",
            usage="intro",
            territory="JP",
            at_job=JOB_DATE,
        )
    assert failure.value.reason == "territory"


def _entry(**overrides: object) -> AssetEntry:
    base: dict[str, object] = {
        "asset_id": "test-asset:tone",
        "sha256": "a" * 64,
        "path": "tests/fixtures/manifests/phase-3/assets/p3-brand-a/tone.wav",
        "usage": "tone",
        "territories": ("WORLDWIDE",),
        "effective_date": "1970-01-01",
        "expiry_date": None,
        "license_evidence_ref": "owner-created:test",
        "attribution": None,
        "content_id_notes": None,
        "approved": True,
    }
    base.update(overrides)
    return AssetEntry.model_validate(base)


def test_expired_asset_blocks() -> None:
    registry = register_assets(_entry(expiry_date="2025-12-31"))
    with pytest.raises(AssetRightsError) as failure:
        check_rights(
            registry,
            "test-asset:tone",
            usage="tone",
            territory=JOB_TERRITORY,
            at_job="2026-01-01",
        )
    assert failure.value.reason == "expired"


def test_not_yet_effective_asset_blocks() -> None:
    registry = register_assets(_entry(effective_date="2026-06-01"))
    with pytest.raises(AssetRightsError) as failure:
        check_rights(
            registry,
            "test-asset:tone",
            usage="tone",
            territory=JOB_TERRITORY,
            at_job="2026-01-01",
        )
    assert failure.value.reason == "expired"


def test_unapproved_asset_blocks() -> None:
    registry = register_assets(_entry(approved=False))
    with pytest.raises(AssetRightsError) as failure:
        check_rights(
            registry,
            "test-asset:tone",
            usage="tone",
            territory=JOB_TERRITORY,
            at_job=JOB_DATE,
        )
    assert failure.value.reason == "unapproved"


def test_changed_asset_bytes_block_rights_gate(registry: RegistrySnapshot) -> None:
    with pytest.raises(AssetRightsError) as failure:
        check_rights(
            registry,
            "p3-brand-a:intro",
            usage="intro",
            territory=JOB_TERRITORY,
            at_job=JOB_DATE,
            observed_sha256="b" * 64,
        )
    assert failure.value.reason == "changed_bytes"


def test_malformed_profile_json_is_refused(brand_a: Phase3FixtureManifest) -> None:
    payload = _system(brand_a).model_dump(mode="json")
    payload["asset_catalog"] = ["not-a-valid identifier!"]

    with pytest.raises(ValidationError):
        SystemPresentationProfile.model_validate(payload)


def test_malformed_registry_entry_is_refused() -> None:
    with pytest.raises(ValidationError):
        AssetEntry.model_validate(
            {
                "asset_id": "bad:entry",
                "sha256": "not-a-hash",
                "path": "",
                "usage": "jingle",
                "territories": (),
                "effective_date": "2026-13-01",
                "expiry_date": None,
                "license_evidence_ref": "",
                "attribution": None,
                "content_id_notes": None,
                "approved": True,
            }
        )


def test_no_profile_self_update_path_exists() -> None:
    forbidden = re.compile(r"\bdef\s+\w*(update|learn|mutate|feedback|rewrite)\w*")
    for source in sorted(Path("services/presentation").glob("*.py")):
        assert not forbidden.search(source.read_text()), (
            f"profile self-update path is forbidden: {source}"
        )


def test_no_internet_fetch_code_exists() -> None:
    network_import = re.compile(r"\b(requests|urllib|httpx|socket|ftplib|aiohttp|http)\b")
    remote_scheme = re.compile(r"://")
    for source in sorted(Path("services/presentation").glob("*.py")):
        text = source.read_text()
        assert not network_import.search(text), f"fetch code is forbidden: {source}"
        assert not remote_scheme.search(text), f"remote URLs are forbidden: {source}"
