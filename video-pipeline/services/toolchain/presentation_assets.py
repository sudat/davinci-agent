"""Presentation-assets toolchain section for Phase 3 (rights-safe A/B brands).

The pin freezes the provenance contract for the two generated brand fixture
packages: every asset byte under the phase-3 fixture directory is produced by
the pinned ffmpeg from lavfi/aevalsrc sources only, carries owner-created
rights metadata, and admits no third-party or production-brand claim. The
smoke is fully deterministic and offline: stored asset bytes are hashed
against the manifest-declared values and every declared recipe argv is
inspected for lavfi-only inputs. No network, no Resolve launch.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel
from services.foundation_io import atomic_write

type AssetKind = Literal["intro", "logo", "outro", "overlay", "se", "tone"]

ALLOWED_RECIPE_SOURCES: tuple[str, ...] = ("color=", "testsrc2", "aevalsrc")
FORBIDDEN_URL_SCHEMES: tuple[str, ...] = ("http://", "https://", "ftp://", "rtsp://")


class PresentationAssetsSection(StrictModel):
    schema_version: Literal["presentation-assets-pin-v1"]
    generator: Literal["pinned-ffmpeg-lavfi-only"]
    brand_package_ids: tuple[Literal["p3-brand-a", "p3-brand-b"], ...] = Field(min_length=2)
    asset_kinds: tuple[AssetKind, ...] = Field(min_length=1)
    manifest_dir: str
    external_credentials: Literal["none"]
    third_party_material: Literal["none"]
    production_brand_claimed: Literal[False] = False

    @model_validator(mode="after")
    def require_both_brands(self) -> PresentationAssetsSection:
        if set(self.brand_package_ids) != {"p3-brand-a", "p3-brand-b"}:
            raise PydanticCustomError(
                "brand_packages", "the pin must cover exactly both brand packages"
            )
        if len(set(self.asset_kinds)) != len(self.asset_kinds):
            raise PydanticCustomError("asset_kinds", "asset kinds must be unique")
        return self


class PresentationAssetsSmokeError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def _manifest_payload(section: PresentationAssetsSection, root: Path, brand_id: str) -> dict:
    path = root / section.manifest_dir / f"{brand_id}.json"
    try:
        payload = json.loads(path.read_bytes())
    except OSError as error:
        raise PresentationAssetsSmokeError(f"brand manifest missing: {brand_id}") from error
    if not isinstance(payload, dict):
        raise PresentationAssetsSmokeError(f"brand manifest malformed: {brand_id}")
    return payload


def _verify_rights(brand_id: str, block: object) -> None:
    if not isinstance(block, dict):
        raise PresentationAssetsSmokeError(f"asset rights block missing: {brand_id}")
    expected = {
        "license": "owner-created",
        "holder": "pipeline-fixture-generator",
        "generated_by": "pinned-ffmpeg-lavfi",
        "third_party_material": "none",
        "production_brand_claimed": False,
    }
    for field, value in expected.items():
        if block.get(field) != value:
            raise PresentationAssetsSmokeError(
                f"rights violation for {brand_id}: {field} must be {value!r}"
            )


def _verify_recipe(brand_id: str, kind: str, recipe: object) -> None:
    if not isinstance(recipe, list) or not recipe:
        raise PresentationAssetsSmokeError(f"recipe argv missing: {brand_id}/{kind}")
    if recipe[0] != "{ffmpeg}" or recipe[-1] != "{out}":
        raise PresentationAssetsSmokeError(
            f"recipe argv must be pinned-ffmpeg with a single output: {brand_id}/{kind}"
        )
    inputs = [recipe[index + 1] for index, element in enumerate(recipe) if element == "-i"]
    if not inputs or not all(
        any(source in value for source in ALLOWED_RECIPE_SOURCES) for value in inputs
    ):
        raise PresentationAssetsSmokeError(
            f"recipe inputs must be lavfi/aevalsrc sources only: {brand_id}/{kind}"
        )
    for element in recipe:
        if isinstance(element, str) and any(scheme in element for scheme in FORBIDDEN_URL_SCHEMES):
            raise PresentationAssetsSmokeError(
                f"network source in recipe is forbidden: {brand_id}/{kind}"
            )


def _verify_brand_assets(
    section: PresentationAssetsSection, root: Path, brand_id: str, payload: dict
) -> dict[str, str]:
    presentation = payload.get("presentation")
    if not isinstance(presentation, dict) or not isinstance(presentation.get("assets"), list):
        raise PresentationAssetsSmokeError(f"asset table missing: {brand_id}")
    kinds: list[str] = []
    hashes: dict[str, str] = {}
    for asset in presentation["assets"]:
        if not isinstance(asset, dict):
            raise PresentationAssetsSmokeError(f"asset row malformed: {brand_id}")
        kind = asset.get("kind")
        if kind not in section.asset_kinds:
            raise PresentationAssetsSmokeError(f"unknown asset kind: {brand_id}/{kind}")
        _verify_rights(brand_id, asset.get("rights"))
        _verify_recipe(brand_id, str(kind), asset.get("recipe"))
        relative = asset.get("path")
        if not isinstance(relative, str):
            raise PresentationAssetsSmokeError(f"asset path missing: {brand_id}/{kind}")
        expected_prefix = f"assets/{brand_id}/"
        if not relative.startswith(expected_prefix):
            raise PresentationAssetsSmokeError(
                f"asset path must live under {expected_prefix}: {brand_id}/{kind}"
            )
        asset_path = root / section.manifest_dir / relative
        try:
            actual = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        except OSError as error:
            raise PresentationAssetsSmokeError(f"asset bytes missing: {brand_id}/{kind}") from error
        if actual != asset.get("sha256"):
            raise PresentationAssetsSmokeError(f"asset bytes drift: {brand_id}/{kind}")
        kinds.append(str(kind))
        hashes[str(kind)] = actual
    if sorted(kinds) != sorted(section.asset_kinds):
        raise PresentationAssetsSmokeError(f"asset inventory incomplete: {brand_id}")
    return hashes


def verify_presentation_assets_contract(
    section: PresentationAssetsSection, root: Path
) -> dict[str, dict[str, str]]:
    if section.external_credentials != "none" or section.third_party_material != "none":
        raise PresentationAssetsSmokeError("presentation-assets pin must carry no externals")
    if section.production_brand_claimed is not False:
        raise PresentationAssetsSmokeError("production-brand claims are forbidden")
    summary: dict[str, dict[str, str]] = {}
    for brand_id in section.brand_package_ids:
        payload = _manifest_payload(section, root, brand_id)
        if payload.get("fixture_id") != brand_id or payload.get("phase") != "phase-3":
            raise PresentationAssetsSmokeError(f"brand manifest binding drift: {brand_id}")
        if payload.get("fixture_only") is not True:
            raise PresentationAssetsSmokeError(f"brand manifest must be fixture-only: {brand_id}")
        summary[brand_id] = _verify_brand_assets(section, root, brand_id, payload)
    return summary


def run_presentation_assets_smoke(
    section: PresentationAssetsSection, root: Path, smoke_dir: Path
) -> Path:
    """Deterministic offline rights/provenance validation; evidence in smoke_dir."""

    smoke_dir.mkdir(parents=True, exist_ok=True)
    summary = verify_presentation_assets_contract(section, root)
    evidence_payload = {
        "asset_sha256s": summary,
        "brand_package_ids": list(section.brand_package_ids),
        "external_credentials": section.external_credentials,
        "generator": section.generator,
        "license": "owner-created",
        "live_rerun": False,
        "network_access": "none",
        "production_brand_claimed": section.production_brand_claimed,
        "third_party_material": section.third_party_material,
    }
    evidence = smoke_dir / "presentation-assets-smoke.json"
    atomic_write(
        evidence,
        json.dumps(evidence_payload, sort_keys=True, separators=(",", ":")).encode(),
    )
    return evidence
