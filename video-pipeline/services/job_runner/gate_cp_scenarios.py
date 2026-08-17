"""Drive the six frozen Control Plane fixture scenarios (Todos 7-12 stack).

Every scenario runs against the REAL components — ``ArtifactStore``
(Todo 7), ``ArtifactRegistry`` (Todo 8), ``StateStore``/CAS/lanes
(Todos 9-10) — inside a fresh scratch directory under the evidence
root, and records raw operation outcomes only. Fault injection uses
the store's own primitives (journal, temp write, no rename) so no
fakes stand in for production paths.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.artifact_registry.reconcile import reconcile as registry_reconcile
from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.reconcile import ReconcileReport, reconcile
from services.artifact_store.store import ArtifactStore
from services.fixtures.manifest_control_plane import (
    CONTROL_PLANE_FIXTURE_IDS,
    ControlPlaneFixtureManifest,
)
from services.job_runner.gate_cp_drive import (
    call_op,
    fresh_dir,
    intent_for,
    make_observation,
    outcome_of,
    write_observation,
)
from services.job_runner.gate_cp_scenario_faults import (
    drive_lease_expiry,
    drive_stale_cas,
    drive_symlink_denial,
)

if TYPE_CHECKING:
    from services.job_runner.gate_cp_models import GateObservation

MANIFEST_DIR: Final = Path("tests/fixtures/manifests/control-plane")
ORPHAN_REGISTRY_ARTIFACT: Final = "cp-orphan-registry-artifact"
ORPHAN_REGISTRY_PAYLOAD: Final = b"control-plane cp-orphan registry adoption payload v1"


class ScenarioDriverError(Exception):
    pass


def load_manifests(manifest_dir: Path) -> dict[str, ControlPlaneFixtureManifest]:
    manifests: dict[str, ControlPlaneFixtureManifest] = {}
    for fixture_id in CONTROL_PLANE_FIXTURE_IDS:
        path = manifest_dir / f"{fixture_id}.json"
        try:
            manifests[fixture_id] = ControlPlaneFixtureManifest.model_validate_json(
                path.read_bytes()
            )
        except (OSError, ValidationError) as error:
            raise ScenarioDriverError(
                f"fixture manifest {path} unreadable/invalid: {error}"
            ) from error
    return manifests


def combined_manifest_sha256(manifest_dir: Path) -> str:
    combined = hashlib.sha256()
    for fixture_id in CONTROL_PLANE_FIXTURE_IDS:
        combined.update((manifest_dir / f"{fixture_id}.json").read_bytes())
    return combined.hexdigest()


def drive_scenarios(evidence: Path, manifest_dir: Path) -> dict[str, Path]:
    """Run all six fixtures; return observation-file paths by fixture id."""

    manifests = load_manifests(manifest_dir)
    drivers: dict[
        str, Callable[[ControlPlaneFixtureManifest, Path], GateObservation]
    ] = {
        "cp-atomic-publish": drive_atomic_publish,
        "cp-crash-before-rename": drive_crash_before_rename,
        "cp-orphan-reconcile": drive_orphan_reconcile,
        "cp-stale-cas": drive_stale_cas,
        "cp-lease-expiry": drive_lease_expiry,
        "cp-path-symlink-denial": drive_symlink_denial,
    }
    written: dict[str, Path] = {}
    for fixture_id, driver in drivers.items():
        work = fresh_dir(evidence, fixture_id)
        written[fixture_id] = write_observation(
            driver(manifests[fixture_id], work), evidence
        )
    return written


def drive_atomic_publish(
    manifest: ControlPlaneFixtureManifest, work: Path
) -> GateObservation:
    store = ArtifactStore(work / "store")
    intent = intent_for(manifest.scenario.artifact_id, manifest.scenario.payload.sha256)
    payload = manifest.scenario.payload.payload_bytes()
    operations = [call_op("publish", store.publish, intent, payload)]
    digest = ""
    try:
        reopened, _ref = store.reopen(intent.envelope.content_hash)
        digest = hashlib.sha256(reopened).hexdigest()
        operations.append(outcome_of("reopen", "ok", digest))
    except Exception as error:  # noqa: BLE001 (raw evidence keeps the code)
        code = getattr(error, "code", type(error).__name__)
        operations.append(outcome_of("reopen", str(code), str(error)))
    return make_observation(
        fixture_id=manifest.fixture_id,
        kind=manifest.scenario.kind,
        work=work,
        operations=operations,
        fields={"reopen_digest": digest},
    )


def drive_crash_before_rename(
    manifest: ControlPlaneFixtureManifest, work: Path
) -> GateObservation:
    store = ArtifactStore(work / "store")
    intent = intent_for(manifest.scenario.artifact_id, manifest.scenario.payload.sha256)
    payload = manifest.scenario.payload.payload_bytes()
    sha = intent.envelope.content_hash
    operations = [
        call_op("journal-intent", store.journal_intent, intent),
        call_op("write-object-temp", store.write_object_temp, sha, payload),
    ]
    report = reconcile(store.store_root)
    operations.append(outcome_of("reconcile", report_action(report)))
    operations.append(call_op("republish", store.publish, intent, payload))
    operations.append(call_op("republish-idempotent", store.publish, intent, payload))
    digest = ""
    try:
        reopened, _ref = store.reopen(sha)
        digest = hashlib.sha256(reopened).hexdigest()
        operations.append(outcome_of("reopen", "ok", digest))
    except Exception as error:  # noqa: BLE001 (raw evidence keeps the code)
        code = getattr(error, "code", type(error).__name__)
        operations.append(outcome_of("reopen", str(code), str(error)))
    return make_observation(
        fixture_id=manifest.fixture_id,
        kind=manifest.scenario.kind,
        work=work,
        operations=operations,
        fields={"reopen_digest": digest, "reconcile_action": report_action(report)},
    )


def drive_orphan_reconcile(
    manifest: ControlPlaneFixtureManifest, work: Path
) -> GateObservation:
    store = ArtifactStore(work / "store")
    payload = manifest.scenario.payload.payload_bytes()
    sha = manifest.scenario.payload.sha256
    shard = store.store_root / "objects" / sha[:2]
    shard.mkdir(parents=True, exist_ok=True)
    (shard / f".obj-{sha}.tmp").write_bytes(payload)
    report = reconcile(store.store_root)
    registry = ArtifactRegistry(work / "registry")
    adoption_intent = intent_for(
        ORPHAN_REGISTRY_ARTIFACT,
        hashlib.sha256(ORPHAN_REGISTRY_PAYLOAD).hexdigest(),
    )
    store.publish(adoption_intent, ORPHAN_REGISTRY_PAYLOAD)
    adopted_report = registry_reconcile(store, registry)
    adopted = (
        "true" if ORPHAN_REGISTRY_ARTIFACT in registry.load().entries else "false"
    )
    operations = [
        outcome_of("plant-orphan-temp", "ok"),
        outcome_of("reconcile", report_action(report)),
        outcome_of("unregistered-publish", "ok"),
        outcome_of("registry-reconcile", f"adopted={len(adopted_report.adopted)}"),
    ]
    return make_observation(
        fixture_id=manifest.fixture_id,
        kind=manifest.scenario.kind,
        work=work,
        operations=operations,
        fields={"reconcile_action": report_action(report), "registry_adopted": adopted},
    )


def report_action(report: ReconcileReport) -> str:
    return report.entries[0].action if report.entries else "no-entries"


__all__ = [
    "MANIFEST_DIR",
    "ORPHAN_REGISTRY_ARTIFACT",
    "ScenarioDriverError",
    "combined_manifest_sha256",
    "drive_atomic_publish",
    "drive_crash_before_rename",
    "drive_orphan_reconcile",
    "drive_scenarios",
    "load_manifests",
    "report_action",
]
