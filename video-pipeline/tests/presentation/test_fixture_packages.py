"""Todo-56 fixture-package acceptance: the two frozen brand manifests.

The two manifests must validate canonically, every stored asset byte must
hash to its declared value under owner-created rights with lavfi-only
recipes, the editorial structure must be identical between the brands, and
the declared presentation-diff dimensions must match the frozen set. All
negative probes run on in-memory COPIES; frozen inputs are never mutated.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.fixtures.manifest_phase3 import (
    PHASE_3_ASSET_KINDS,
    PHASE_3_FIXTURE_IDS,
    Phase3FixtureManifest,
    editorial_structure_bytes,
)

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-3")

EXPECTED_DIFF_DIMENSIONS = (
    "asset.intro.sha256",
    "asset.logo.sha256",
    "asset.outro.sha256",
    "asset.overlay.sha256",
    "asset.se.sha256",
    "asset.tone.sha256",
    "audio.intro_tone_hz",
    "audio.outro_tone_hz",
    "audio.se_hz",
    "audio.tone_hz",
    "color.accent_hex",
    "color.neutral_hex",
    "color.primary_hex",
    "style.background_opacity_percent",
    "style.font_size_px",
    "style.margin_bottom_px",
    "style.outline_color_hex",
    "style.outline_width_px",
    "style.primary_color_hex",
)


def _load(fixture_id: str) -> Phase3FixtureManifest:
    return Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
    )


def _payload(fixture_id: str) -> dict[str, object]:
    return json.loads((MANIFEST_DIR / f"{fixture_id}.json").read_bytes())


def _assets_of(payload: dict[str, object]) -> list[dict[str, object]]:
    presentation = payload["presentation"]
    assert isinstance(presentation, dict)
    assets = presentation["assets"]
    assert isinstance(assets, list)
    rows: list[dict[str, object]] = []
    for row in assets:
        assert isinstance(row, dict)
        rows.append(row)
    return rows


def _rights_of(asset: dict[str, object]) -> dict[str, object]:
    rights = asset["rights"]
    assert isinstance(rights, dict)
    return rights


def _records_of(payload: dict[str, object]) -> list[dict[str, object]]:
    editorial = payload["editorial"]
    assert isinstance(editorial, dict)
    records = editorial["base_records"]
    assert isinstance(records, list)
    rows: list[dict[str, object]] = []
    for row in records:
        assert isinstance(row, dict)
        rows.append(row)
    return rows


def test_phase3_product_module_follows_the_frozen_inputs() -> None:
    """Post-implementation successor of the pre-freeze module guard (Todo 55).

    At freeze time (Todo 56) no Phase-3 product module was allowed to exist.
    Implementation has since landed, so the durable invariant is that the
    module exists ONLY on top of the still-unchanged frozen inputs.
    """

    assert (Path.cwd().resolve() / "services" / "presentation").is_dir()
    for fixture_id in PHASE_3_FIXTURE_IDS:
        manifest = _load(fixture_id)
        assert manifest.fixture_only is True
        assert manifest.expectation_basis == "pre-registered-declared-presentation-diff"


def test_both_fixture_manifests_validate_and_are_canonical() -> None:
    for fixture_id in PHASE_3_FIXTURE_IDS:
        raw = (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
        manifest = Phase3FixtureManifest.model_validate_json(raw)

        assert manifest.fixture_id == fixture_id
        assert manifest.phase == "phase-3"
        assert manifest.fixture_only is True
        assert manifest.expectation_basis == "pre-registered-declared-presentation-diff"
        assert raw == manifest.canonical_bytes()


def test_stored_asset_bytes_match_declared_hashes_with_owner_rights() -> None:
    for fixture_id in PHASE_3_FIXTURE_IDS:
        manifest = _load(fixture_id)
        kinds = sorted(asset.kind for asset in manifest.presentation.assets)
        assert kinds == sorted(PHASE_3_ASSET_KINDS)
        for asset in manifest.presentation.assets:
            path = MANIFEST_DIR / asset.path
            assert path.is_file(), f"missing asset bytes: {fixture_id}/{asset.kind}"
            assert hashlib.sha256(path.read_bytes()).hexdigest() == asset.sha256
            rights = asset.rights
            assert rights.license == "owner-created"
            assert rights.holder == "pipeline-fixture-generator"
            assert rights.generated_by == "pinned-ffmpeg-lavfi"
            assert rights.third_party_material == "none"
            assert rights.production_brand_claimed is False


def test_editorial_structures_are_identical_between_brands() -> None:
    manifest_a = _load("p3-brand-a")
    manifest_b = _load("p3-brand-b")

    assert manifest_a.editorial.editorial_structure_sha256 == (
        manifest_b.editorial.editorial_structure_sha256
    )
    assert editorial_structure_bytes(manifest_a.editorial) == editorial_structure_bytes(
        manifest_b.editorial
    )
    assert manifest_a.editorial.derived_from == "p1-ref-01-clean-ja"
    assert manifest_a.editorial.declared_media.sha256 == (
        manifest_b.editorial.declared_media.sha256
    )


def test_declared_diff_dimensions_match_the_frozen_set() -> None:
    for fixture_id in PHASE_3_FIXTURE_IDS:
        manifest = _load(fixture_id)

        assert tuple(manifest.declared_diff.dimensions) == EXPECTED_DIFF_DIMENSIONS
        assert manifest.declared_diff.editorial_invariant is True


def test_unknown_asset_kind_is_refused() -> None:
    payload = _payload("p3-brand-a")
    _assets_of(payload)[0]["kind"] = "jingle"

    with pytest.raises(ValidationError, match="literal_error"):
        Phase3FixtureManifest.model_validate(payload)


def test_missing_asset_license_is_refused() -> None:
    payload = _payload("p3-brand-a")
    del _rights_of(_assets_of(payload)[0])["license"]

    with pytest.raises(ValidationError, match="license"):
        Phase3FixtureManifest.model_validate(payload)


def test_production_brand_claim_is_refused() -> None:
    payload = _payload("p3-brand-b")
    _rights_of(_assets_of(payload)[1])["production_brand_claimed"] = True

    with pytest.raises(ValidationError, match="production_brand_claimed"):
        Phase3FixtureManifest.model_validate(payload)


def test_third_party_license_claim_is_refused() -> None:
    payload = _payload("p3-brand-a")
    _rights_of(_assets_of(payload)[2])["license"] = "cc-by-4.0"

    with pytest.raises(ValidationError, match="owner-created"):
        Phase3FixtureManifest.model_validate(payload)


def test_editorial_difference_between_brands_is_refused() -> None:
    payload = _payload("p3-brand-b")
    _records_of(payload)[0]["record_end"] = 149

    with pytest.raises(ValidationError, match="editorial_hash"):
        Phase3FixtureManifest.model_validate(payload)


def test_recipe_with_file_or_network_input_is_refused() -> None:
    payload = _payload("p3-brand-a")
    _assets_of(payload)[0]["recipe"] = [
        "{ffmpeg}",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-i",
        "https://cdn.example.com/intro.mov",
        "{out}",
    ]

    with pytest.raises(ValidationError, match=r"recipe_source|recipe_network"):
        Phase3FixtureManifest.model_validate(payload)
