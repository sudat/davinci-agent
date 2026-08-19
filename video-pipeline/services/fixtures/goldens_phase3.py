"""Golden binding checks for the phase-3 freeze."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from services.fixtures.golden import GoldenAuditError, audit_derivation_source
from services.fixtures.manifest_phase3 import (
    PHASE_3_FIXTURE_IDS,
    EditorialBase,
    Phase3FixtureManifest,
)
from services.fixtures.models import GoldenHashes
from services.fixtures.prepare import PrepareError
from services.foundation_io import sha256_file

PHASE_3_GOLDEN_DIR = Path("tests/goldens/reference/phase-3")
PHASE_1_GOLDEN_EXPECTED = Path("tests/goldens/reference/phase-1-technical/expected.json")
PHASE_2_LINEAGE_MANIFEST = Path("tests/fixtures/manifests/phase-2/p2-stale-capability.json")
PHASE_3_MANIFEST_DIR = Path("tests/fixtures/manifests/phase-3")


def _canonical_json_file(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if raw != canonical.encode():
        raise PrepareError(f"Golden artifact is noncanonical: {path.name}")
    return payload


def _assert_editorial_invariance(manifests: dict[str, bytes]) -> str:
    parsed = {
        fixture_id: Phase3FixtureManifest.model_validate_json(raw)
        for fixture_id, raw in manifests.items()
    }
    base_a = parsed["p3-brand-a"].editorial
    base_b = parsed["p3-brand-b"].editorial
    if base_a.editorial_structure_sha256 != base_b.editorial_structure_sha256:
        raise PrepareError("editorial structure hashes differ between the brands")
    if EditorialBase.model_dump(base_a, exclude={"editorial_structure_sha256"}) != (
        EditorialBase.model_dump(base_b, exclude={"editorial_structure_sha256"})
    ):
        raise PrepareError("editorial structures differ between the brands")
    return base_a.editorial_structure_sha256


_GOLDEN_ROW_FIELDS = (
    "item_id",
    "kind",
    "track_index",
    "record_start",
    "record_end",
    "source_start",
    "source_end",
    "subtitle_text",
)


def _golden_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{field: row[field] for field in _GOLDEN_ROW_FIELDS} for row in rows]


def _assert_phase1_derivation(expected_p1: dict[str, Any], manifests: dict[str, bytes]) -> None:
    fixture = expected_p1.get("fixtures", {}).get("p1-ref-01-clean-ja")
    if not isinstance(fixture, dict):
        raise PrepareError("Phase-1 golden table is missing p1-ref-01-clean-ja")
    golden_rows = _golden_rows(fixture["ir_records"])
    lineage = json.loads(PHASE_2_LINEAGE_MANIFEST.read_bytes())
    if _golden_rows(lineage["base"]["base_records"]) != golden_rows:
        raise PrepareError("phase-2 lineage manifest drifted from the Phase-1 golden")
    for fixture_id, raw in manifests.items():
        manifest = Phase3FixtureManifest.model_validate_json(raw)
        declared = _golden_rows(
            [dict(row) for row in manifest.editorial.model_dump(mode="json")["base_records"]]
        )
        if declared != golden_rows:
            raise PrepareError(f"editorial base drifts from the Phase-1 golden: {fixture_id}")
        media = manifest.editorial.declared_media
        lineage_media = lineage["base"]["declared_media"]
        if (media.sha256, media.source_id, media.duration_frames) != (
            lineage_media["sha256"],
            lineage_media["source_id"],
            lineage_media["duration_frames"],
        ):
            raise PrepareError(f"edit-source binding drift: {fixture_id}")


def _assert_golden_agreement(expected: dict[str, Any], manifests: dict[str, bytes]) -> None:
    ab_diff = expected.get("ab_diff")
    if not isinstance(ab_diff, dict):
        raise PrepareError("Golden A/B diff table is missing")
    declared_dimensions = ab_diff.get("declared_dimensions")
    if not isinstance(declared_dimensions, list):
        raise PrepareError("Golden declared-dimension list is missing")
    fixtures = expected.get("fixtures")
    if not isinstance(fixtures, dict) or set(fixtures) != set(PHASE_3_FIXTURE_IDS):
        raise PrepareError("Golden fixture table is incomplete")
    for fixture_id, raw in manifests.items():
        manifest = Phase3FixtureManifest.model_validate_json(raw)
        golden = fixtures[fixture_id]
        assets = {asset.kind: asset.sha256 for asset in manifest.presentation.assets}
        if golden.get("assets") != assets:
            raise PrepareError(f"manifest/golden asset table drift: {fixture_id}")
        if golden.get("editorial_structure_sha256") != (
            manifest.editorial.editorial_structure_sha256
        ):
            raise PrepareError(f"manifest/golden editorial hash drift: {fixture_id}")
        if tuple(declared_dimensions) != manifest.declared_diff.dimensions:
            raise PrepareError(f"manifest/golden diff-dimension drift: {fixture_id}")
    for kind, row in ab_diff.get("asset_sha256", {}).items():
        if not isinstance(row, dict) or row.get("differ") is not True:
            raise PrepareError(f"Golden expects every asset hash to differ: {kind}")


def phase3_golden_hashes(root: Path, manifests: dict[str, bytes]) -> GoldenHashes:
    phase_dir = root / PHASE_3_GOLDEN_DIR
    source = phase_dir / "derive.py"
    expected = phase_dir / "expected.json"
    audit = phase_dir / "import-audit.json"
    index = phase_dir / "index.json"
    audited = audit_derivation_source(source)
    audit_payload = _canonical_json_file(audit)
    if audit_payload.get("audit_result") != "pass":
        raise PrepareError("frozen Golden import audit did not pass")
    if audit_payload.get("derivation_source_sha256") != audited.derivation_source_sha256:
        raise PrepareError("Golden derivation source hash drift")
    index_payload = _canonical_json_file(index)
    bindings = {
        "audit_sha256": sha256_file(audit),
        "derivation_source_sha256": sha256_file(source),
        "expected_sha256": sha256_file(expected),
        "fixture_manifest_sha256s": {
            fixture_id: hashlib.sha256(raw).hexdigest() for fixture_id, raw in manifests.items()
        },
        "phase2_lineage_manifest_sha256": sha256_file(root / PHASE_2_LINEAGE_MANIFEST),
    }
    if any(index_payload.get(key) != value for key, value in bindings.items()):
        raise PrepareError("Golden index binding drift")
    _assert_phase1_derivation(json.loads((root / PHASE_1_GOLDEN_EXPECTED).read_bytes()), manifests)
    _assert_editorial_invariance(manifests)
    _assert_golden_agreement(json.loads(expected.read_bytes()), manifests)
    return GoldenHashes(
        derivation_source_sha256=sha256_file(source),
        expected_sha256=sha256_file(expected),
        audit_sha256=sha256_file(audit),
        index_sha256=sha256_file(index),
    )


__all__ = ["GoldenAuditError", "phase3_golden_hashes"]
