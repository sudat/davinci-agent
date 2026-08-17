"""Fault-injected Control Plane scenario drivers (stale CAS, lease, symlink).

``cp-stale-cas`` tampers object bytes after publication and proves
both the store reopen rejection and the Todo-9 State-vs-Artifact
fail-closed verify. ``cp-lease-expiry`` exercises the flock lease
authority per the frozen manifest plus the Todo-10 SQL-lane expiry.
``cp-path-symlink-denial`` refuses publication through a symlinked
store component.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_registry.store_view import object_path
from services.artifact_store.lease import LeaseAuthority
from services.artifact_store.store import ArtifactStore, StoreRefusalError
from services.job_runner.gate_cp_drive import (
    call_op,
    intent_for,
    make_observation,
    outcome_of,
)
from services.job_runner.gate_cp_state import (
    drive_lease_expiry_lanes,
    drive_stale_state_disagreement,
)

if TYPE_CHECKING:
    from services.fixtures.manifest_control_plane import ControlPlaneFixtureManifest
    from services.job_runner.gate_cp_models import GateObservation


class LeaseDriverError(Exception):
    pass


def drive_stale_cas(
    manifest: ControlPlaneFixtureManifest, work: Path
) -> GateObservation:
    store = ArtifactStore(work / "store")
    registry = ArtifactRegistry(work / "registry")
    intent = intent_for(manifest.scenario.artifact_id, manifest.scenario.payload.sha256)
    payload = manifest.scenario.payload.payload_bytes()
    sha = intent.envelope.content_hash
    try:
        receipt = store.publish(intent, payload)
        operations = [outcome_of("publish", "ok")]
        registry.register(store, receipt)
        operations.append(outcome_of("register", "ok"))
    except StoreRefusalError as error:
        return make_observation(
            fixture_id=manifest.fixture_id,
            kind=manifest.scenario.kind,
            work=work,
            operations=[outcome_of("publish", error.code, str(error))],
        )
    object_path(store.store_root, sha).write_bytes(b"tampered-cas-bytes")
    operations.append(outcome_of("tamper-object", "ok"))
    try:
        store.reopen(sha)
        operations.append(outcome_of("reopen", "unexpected-success"))
    except StoreRefusalError as error:
        operations.append(outcome_of("reopen", error.code, str(error)))
    operations.append(drive_stale_state_disagreement(work / "state", store, registry, sha))
    return make_observation(
        fixture_id=manifest.fixture_id,
        kind=manifest.scenario.kind,
        work=work,
        operations=operations,
    )


def drive_lease_expiry(
    manifest: ControlPlaneFixtureManifest, work: Path
) -> GateObservation:
    lease = manifest.scenario.lease
    if lease is None:
        raise LeaseDriverError("lease-expiry manifest lacks a lease spec")
    authority = LeaseAuthority(work / "leases")
    now = 1_000_000
    operations = [
        call_op(
            "acquire-lease",
            authority.acquire,
            "cp-baseline",
            lease.holder,
            now_unix=now,
            ttl_seconds=lease.ttl_seconds,
        ),
    ]
    expired_now = now + lease.ttl_seconds + lease.elapsed_past_expiry_seconds
    operations.extend(
        [
            call_op(
                "expired-commit",
                authority.commit,
                "cp-baseline",
                lease.holder,
                now_unix=expired_now,
            ),
            call_op(
                "new-holder-acquire",
                authority.acquire,
                "cp-baseline",
                lease.challenger,
                now_unix=expired_now,
                ttl_seconds=lease.ttl_seconds,
            ),
            call_op(
                "new-holder-commit",
                authority.commit,
                "cp-baseline",
                lease.challenger,
                now_unix=expired_now,
            ),
        ]
    )
    operations.extend(drive_lease_expiry_lanes(work / "state"))
    return make_observation(
        fixture_id=manifest.fixture_id,
        kind=manifest.scenario.kind,
        work=work,
        operations=operations,
    )


def drive_symlink_denial(
    manifest: ControlPlaneFixtureManifest, work: Path
) -> GateObservation:
    store = ArtifactStore(work / "store")
    intent = intent_for(manifest.scenario.artifact_id, manifest.scenario.payload.sha256)
    payload = manifest.scenario.payload.payload_bytes()
    sha = intent.envelope.content_hash
    outside = work / "outside"
    outside.mkdir()
    shard = store.store_root / "objects" / sha[:2]
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.symlink_to(outside)
    operations = [
        outcome_of("plant-symlink", "ok"),
        call_op("publish", store.publish, intent, payload),
    ]
    return make_observation(
        fixture_id=manifest.fixture_id,
        kind=manifest.scenario.kind,
        work=work,
        operations=operations,
    )


__all__ = [
    "LeaseDriverError",
    "drive_lease_expiry",
    "drive_stale_cas",
    "drive_symlink_denial",
]
