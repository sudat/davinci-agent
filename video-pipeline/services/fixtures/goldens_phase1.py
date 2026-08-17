"""Golden binding checks for the phase-1-technical freeze."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from services.fixtures.golden import GoldenAuditError, audit_derivation_source
from services.fixtures.manifest_phase1 import Phase1TechnicalFixtureManifest
from services.fixtures.models import GoldenHashes
from services.fixtures.prepare import PrepareError
from services.foundation_io import sha256_file

PHASE_1_GOLDEN_DIR = Path("tests/goldens/reference/phase-1-technical")


def _canonical_json_file(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if raw != canonical.encode():
        raise PrepareError(f"Golden artifact is noncanonical: {path.name}")
    return payload


def _assert_golden_agreement(
    expected: dict[str, Any],
    manifests: dict[str, bytes],
) -> None:
    fixtures = expected.get("fixtures")
    transcripts = expected.get("transcript_spans")
    if not isinstance(fixtures, dict) or not isinstance(transcripts, dict):
        raise PrepareError("Golden expected table is malformed")
    for fixture_id, raw in manifests.items():
        golden = fixtures.get(fixture_id)
        spans = transcripts.get(fixture_id)
        if not isinstance(golden, dict) or not isinstance(spans, list):
            raise PrepareError(f"Golden expected table is missing {fixture_id}")
        manifest = Phase1TechnicalFixtureManifest.model_validate_json(raw)
        if manifest.expected != golden:
            raise PrepareError(f"manifest/golden expected drift: {fixture_id}")
        declared_spans = [
            {
                "segment_id": segment.segment_id,
                "text": segment.text,
                "start_frame": segment.span.start_frame,
                "end_frame": segment.span.end_frame,
            }
            for segment in manifest.transcript.segments
        ]
        if declared_spans != spans:
            raise PrepareError(f"manifest/golden transcript-span drift: {fixture_id}")
        if manifest.analyzer_expectations != golden.get("analyzer_expectations"):
            raise PrepareError(f"manifest/golden analyzer drift: {fixture_id}")


def phase1_golden_hashes(root: Path, manifests: dict[str, bytes]) -> GoldenHashes:
    phase_dir = root / PHASE_1_GOLDEN_DIR
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
    _assert_golden_agreement(json.loads(expected.read_bytes()), manifests)
    return GoldenHashes(
        derivation_source_sha256=sha256_file(source),
        expected_sha256=sha256_file(expected),
        audit_sha256=sha256_file(audit),
        index_sha256=sha256_file(index),
    )


__all__ = ["GoldenAuditError", "phase1_golden_hashes"]
