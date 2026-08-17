from __future__ import annotations

import hashlib
from pathlib import Path

from services.artifact_store.models import PublicationIntent
from services.artifact_store.reconcile import ReconcileReport
from services.contracts.primitives import (
    ArtifactEnvelope,
    Producer,
)
from services.fixtures.manifest_control_plane import (
    CONTROL_PLANE_FIXTURE_IDS,
    ControlPlaneFixtureManifest,
)

MANIFEST_DIR = Path("tests/fixtures/manifests/control-plane")


def load_manifest(fixture_id: str) -> ControlPlaneFixtureManifest:
    raw = (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
    manifest = ControlPlaneFixtureManifest.model_validate_json(raw)
    assert raw == manifest.canonical_bytes()
    return manifest


def load_all_manifests() -> dict[str, ControlPlaneFixtureManifest]:
    return {fixture_id: load_manifest(fixture_id) for fixture_id in CONTROL_PLANE_FIXTURE_IDS}


def payload_for(manifest: ControlPlaneFixtureManifest) -> bytes:
    return manifest.scenario.payload.payload_bytes()


def intent_for(manifest: ControlPlaneFixtureManifest) -> PublicationIntent:
    payload = payload_for(manifest)
    return PublicationIntent(
        envelope=ArtifactEnvelope(
            artifact_id=manifest.scenario.artifact_id,
            artifact_type="control-plane-fixture",
            schema_version="fixture-v1",
            content_hash=hashlib.sha256(payload).hexdigest(),
            producer=Producer(name="cp-fixture-producer", version="v1"),
            inputs=(),
        )
    )


def object_path(store_root: Path, content_sha256: str) -> Path:
    return store_root / "objects" / content_sha256[:2] / content_sha256


def temp_path(store_root: Path, content_sha256: str) -> Path:
    return store_root / "objects" / content_sha256[:2] / f".obj-{content_sha256}.tmp"


def meta_path(store_root: Path, artifact_id: str) -> Path:
    return store_root / "artifacts" / f"{artifact_id}.json"


def journal_path(store_root: Path, content_sha256: str) -> Path:
    return store_root / "journal" / f"{content_sha256}.json"


def reconcile_log_lines(store_root: Path) -> list[ReconcileReport]:
    log = store_root / "reconcile-log.jsonl"
    if not log.exists():
        return []
    return [
        ReconcileReport.model_validate_json(line)
        for line in log.read_text().splitlines()
        if line
    ]
