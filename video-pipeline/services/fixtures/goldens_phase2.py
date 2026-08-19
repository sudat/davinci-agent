"""Golden binding checks for the phase-2 freeze."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from services.fixtures.golden import GoldenAuditError, audit_derivation_source
from services.fixtures.manifest_phase2 import PHASE_2_DERIVED_FROM, Phase2FixtureManifest
from services.fixtures.models import GoldenHashes
from services.fixtures.prepare import PrepareError
from services.foundation_io import sha256_file

PHASE_2_GOLDEN_DIR = Path("tests/goldens/reference/phase-2")
PHASE_1_GOLDEN_EXPECTED = Path("tests/goldens/reference/phase-1-technical/expected.json")
PHASE_1_MANIFEST_DIR = Path("tests/fixtures/manifests/phase-1-technical")

def _canonical_json_file(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if raw != canonical.encode():
        raise PrepareError(f"Golden artifact is noncanonical: {path.name}")
    return payload


def _assert_phase1_derivation(expected_p1: dict[str, Any], manifests: dict[str, bytes]) -> None:
    """Every declared base table must equal the frozen Phase-1 golden values."""

    for fixture_id, raw in manifests.items():
        manifest = Phase2FixtureManifest.model_validate_json(raw)
        p1_id = PHASE_2_DERIVED_FROM[fixture_id]
        fixture = expected_p1.get("fixtures", {}).get(p1_id)
        if not isinstance(fixture, dict):
            raise PrepareError(f"Phase-1 golden table is missing {p1_id}")
        if manifest.base.derived_from != p1_id:
            raise PrepareError(f"fixture manifest derivation drift: {fixture_id}")
        golden_records = _golden_records(fixture["ir_records"])
        declared_records = _declared_records(manifest)
        if declared_records != golden_records:
            raise PrepareError(f"declared base records drift from the Phase-1 golden: {fixture_id}")


def _golden_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "item_id": row["item_id"],
            "kind": row["kind"],
            "track_index": row["track_index"],
            "record_start": row["record_start"],
            "record_end": row["record_end"],
            "source_start": row["source_start"],
            "source_end": row["source_end"],
            "subtitle_text": row["subtitle_text"],
        }
        for row in rows
    ]


def _declared_records(manifest: Phase2FixtureManifest) -> list[dict[str, Any]]:
    return [
        {
            "item_id": row.item_id,
            "kind": row.kind,
            "track_index": row.track_index,
            "record_start": row.record_start,
            "record_end": row.record_end,
            "source_start": row.source_start,
            "source_end": row.source_end,
            "subtitle_text": row.subtitle_text,
        }
        for row in manifest.base.base_records
    ]


def _assert_route_agreement(
    fixture_id: str, manifest: Phase2FixtureManifest, route: dict[str, Any]
) -> None:
    fault_view = manifest.fault.model_dump(mode="json")
    if route.get("package_compilation") != manifest.fault.expected_package_compilation:
        raise PrepareError(f"manifest/golden route drift: {fixture_id}")
    if route.get("retry") != manifest.fault.expected_retry:
        raise PrepareError(f"manifest/golden retry drift: {fixture_id}")
    if route.get("human_route") != manifest.fault.expected_human_route:
        raise PrepareError(f"manifest/golden human-route drift: {fixture_id}")
    if route.get("failure_code") != fault_view.get("expected_failure_code"):
        raise PrepareError(f"manifest/golden failure-code drift: {fixture_id}")


def _assert_package_agreement(
    fixture_id: str, manifest: Phase2FixtureManifest, package: dict[str, Any]
) -> None:
    if not isinstance(package, dict) or not package.get("placements"):
        raise PrepareError(f"Golden package table is missing {fixture_id}")
    placed = {row["item_id"] for row in package["placements"]}
    av_rows = {
        row.item_id for row in manifest.base.base_records if row.kind in ("video", "audio")
    }
    if placed != av_rows:
        raise PrepareError(f"manifest/golden placement drift: {fixture_id}")
    declared_view = manifest.base.declared_media.model_dump(mode="json")
    if package.get("inputs_view", {}).get("declared_media") != declared_view:
        raise PrepareError(f"manifest/golden media binding drift: {fixture_id}")


def _assert_golden_agreement(expected: dict[str, Any], manifests: dict[str, bytes]) -> None:
    fixtures = expected.get("fixtures")
    if not isinstance(fixtures, dict):
        raise PrepareError("Golden expected table is malformed")
    for fixture_id, raw in manifests.items():
        golden = fixtures.get(fixture_id)
        if not isinstance(golden, dict):
            raise PrepareError(f"Golden expected table is missing {fixture_id}")
        manifest = Phase2FixtureManifest.model_validate_json(raw)
        if manifest.base.derived_from != golden.get("derived_from"):
            raise PrepareError(f"manifest/golden derivation drift: {fixture_id}")
        if manifest.fault.model_dump(mode="json") != golden.get("fault"):
            raise PrepareError(f"manifest/golden fault drift: {fixture_id}")
        route = golden.get("expected_route")
        if not isinstance(route, dict):
            raise PrepareError(f"Golden routing table is missing {fixture_id}")
        _assert_route_agreement(fixture_id, manifest, route)
        _assert_package_agreement(fixture_id, manifest, golden.get("package", {}))


def phase2_golden_hashes(root: Path, manifests: dict[str, bytes]) -> GoldenHashes:
    phase_dir = root / PHASE_2_GOLDEN_DIR
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
    }
    if any(index_payload.get(key) != value for key, value in bindings.items()):
        raise PrepareError("Golden index binding drift")
    expected_p1 = json.loads((root / PHASE_1_GOLDEN_EXPECTED).read_bytes())
    _assert_phase1_derivation(expected_p1, manifests)
    _assert_golden_agreement(json.loads(expected.read_bytes()), manifests)
    return GoldenHashes(
        derivation_source_sha256=sha256_file(source),
        expected_sha256=sha256_file(expected),
        audit_sha256=sha256_file(audit),
        index_sha256=sha256_file(index),
    )


__all__ = ["GoldenAuditError", "phase2_golden_hashes"]
