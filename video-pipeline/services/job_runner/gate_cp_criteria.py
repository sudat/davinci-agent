"""Criterion check functions for the Control Plane Baseline Gate.

Each ``check_*`` recomputes one frozen criterion (or the shared policy
bindings) from raw observation files plus independently recomputed
filesystem state; see ``gate_cp_evaluate`` for the criterion mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import sha256_file
from services.job_runner.gate_cp_checks import (
    ATOMIC,
    CheckState,
    GateMismatch,
    meta_present,
    object_digest,
    reconcile_actions,
    registry_has,
    temp_present,
)
from services.job_runner.gate_cp_scenarios import (
    ORPHAN_REGISTRY_ARTIFACT,
    combined_manifest_sha256,
)

if TYPE_CHECKING:
    from services.fixtures.manifest_control_plane import ControlPlaneFixtureManifest
    from services.gates import GatePolicy
    from services.job_runner.gate_cp_models import GateObservation


def check_bindings(
    policy: GatePolicy,
    manifest_dir: Path,
    toolchain_lock: Path,
    parent_result: Path,
    state: CheckState,
) -> None:
    if policy.fixture_manifest_sha256 != combined_manifest_sha256(manifest_dir):
        state.fail(
            ATOMIC,
            "fixture-manifest-drift",
            "manifest bytes do not hash to the frozen policy binding",
        )
    if policy.toolchain_lock_sha256 is None or policy.toolchain_lock_sha256 != sha256_file(
        toolchain_lock
    ):
        state.fail(ATOMIC, "toolchain-lock-drift", "toolchain lock bytes drift")
    parents = tuple(policy.parent_gate_result_hashes)
    if len(parents) != 1 or sha256_file(parent_result) != parents[0]:
        state.fail(
            ATOMIC,
            "parent-gate-drift",
            f"parent gate result {parent_result} does not match the frozen binding",
        )


def _payload_sha(manifest: ControlPlaneFixtureManifest) -> str:
    return manifest.scenario.payload.sha256


def check_atomic(
    atomic_manifest: ControlPlaneFixtureManifest,
    symlink_manifest: ControlPlaneFixtureManifest,
    observations: dict[str, GateObservation],
    state: CheckState,
) -> None:
    criterion = ATOMIC
    sha = _payload_sha(atomic_manifest)
    artifact_id = atomic_manifest.scenario.artifact_id
    observation = observations.get("cp-atomic-publish")
    symlink_observation = observations.get("cp-path-symlink-denial")
    approvals = observations.get("cp-approvals")
    if observation is None or approvals is None or symlink_observation is None:
        state.fail(criterion, "observation-missing", "atomic/approvals observation absent")
        return
    state.op(criterion, observation, "publish", "ok")
    state.op(criterion, observation, "reopen", "ok")
    if observation.field("reopen_digest") != sha:
        state.fail(criterion, "reopen-digest-mismatch", "reopen bytes differ from manifest")
    work = Path(observation.work_dir)
    if object_digest(work, sha) != sha:
        state.fail(criterion, "object-bytes-drift", "stored object hash != manifest payload")
    if not meta_present(work, artifact_id):
        state.fail(criterion, "meta-absent", "artifact meta sidecar missing")
    if temp_present(work, sha):
        state.fail(criterion, "temp-present", "in-flight temp survived publication")
    symlink_sha = _payload_sha(symlink_manifest)
    state.op(criterion, symlink_observation, "plant-symlink", "ok")
    state.op(criterion, symlink_observation, "publish", "symlinked-path-component")
    if object_digest(Path(symlink_observation.work_dir), symlink_sha) is not None:
        state.fail(criterion, "symlink-publication-leaked", "refused publication wrote bytes")
    state.op(criterion, approvals, "automation-ingress-refused", "automation-refused")
    state.op(criterion, approvals, "wrong-purpose-target-model-rejected", "validation-error")
    state.op(criterion, approvals, "editorial-reused-as-presentation", "purpose-target-mismatch")
    state.op(criterion, approvals, "operator-gate-fixture-refused", "fixture-record")
    if approvals.field("fixture_gate_authorized") != "true":
        state.fail(
            criterion,
            "fixture-record-unauthorized",
            "fixture seam record must pass automated gates",
        )
    if approvals.field("fixture_record_fixture_only") != "true":
        state.fail(
            criterion,
            "fixture-label-missing",
            "gate-created records must stay fixture-marked",
        )


def check_crash(
    crash_manifest: ControlPlaneFixtureManifest,
    orphan_manifest: ControlPlaneFixtureManifest,
    observations: dict[str, GateObservation],
    state: CheckState,
) -> None:
    criterion = "phase-1-cp-crash-reconciliation"
    crash = observations.get("cp-crash-before-rename")
    orphan = observations.get("cp-orphan-reconcile")
    if crash is None or orphan is None:
        state.fail(criterion, "observation-missing", "crash/orphan observation absent")
        return
    crash_sha = _payload_sha(crash_manifest)
    state.op(criterion, crash, "journal-intent", "ok")
    state.op(criterion, crash, "write-object-temp", "ok")
    state.op(criterion, crash, "reconcile", "discard-crashed-temp")
    if "discard-crashed-temp" not in reconcile_actions(Path(crash.work_dir)):
        state.fail(criterion, "reconcile-log-missing", "no discard-crashed-temp log entry")
    if object_digest(Path(crash.work_dir), crash_sha) != crash_sha:
        state.fail(criterion, "crash-final-object-drift", "republished object bytes drift")
    orphan_sha = _payload_sha(orphan_manifest)
    orphan_work = Path(orphan.work_dir)
    state.op(criterion, orphan, "reconcile", "discard-orphan-temp")
    if temp_present(orphan_work, orphan_sha) or object_digest(orphan_work, orphan_sha):
        state.fail(criterion, "orphan-leftovers", "temp/object survived orphan reconcile")
    if len(reconcile_actions(orphan_work)) != 1:
        state.fail(criterion, "orphan-log-count", "expected exactly one reconcile entry")
    state.op(criterion, orphan, "registry-reconcile", "adopted=1")
    if not registry_has(orphan_work, ORPHAN_REGISTRY_ARTIFACT):
        state.fail(criterion, "registry-adoption-missing", "orphan artifact not adopted")


def check_lease(
    observations: dict[str, GateObservation],
    state: CheckState,
) -> None:
    criterion = "phase-1-cp-lease-authority"
    observation = observations.get("cp-lease-expiry")
    approvals = observations.get("cp-approvals")
    if observation is None or approvals is None:
        state.fail(criterion, "observation-missing", "lease/approvals observation absent")
        return
    state.op(criterion, observation, "acquire-lease", "ok")
    state.op(criterion, observation, "expired-commit", "lease-expired")
    state.op(criterion, observation, "new-holder-acquire", "ok")
    state.op(criterion, observation, "new-holder-commit", "ok")
    state.op(criterion, observation, "lane-holder-a-apply", "ok")
    state.op(criterion, observation, "lane-expired-holder-refused", "lease-expired")
    state.op(criterion, observation, "lane-new-holder-commit", "ok")
    state.op(criterion, approvals, "automation-ingress-refused", "automation-refused")
    state.op(criterion, approvals, "non-tty-ingress-refused", "not-a-tty")


def check_stale(
    stale_manifest: ControlPlaneFixtureManifest,
    observations: dict[str, GateObservation],
    state: CheckState,
) -> None:
    criterion = "phase-1-cp-stale-cas-suppression"
    observation = observations.get("cp-stale-cas")
    approvals = observations.get("cp-approvals")
    if observation is None or approvals is None:
        state.fail(criterion, "observation-missing", "stale/approvals observation absent")
        return
    sha = _payload_sha(stale_manifest)
    state.op(criterion, observation, "publish", "ok")
    state.op(criterion, observation, "reopen", "content-hash-mismatch")
    state.op(criterion, observation, "state-verify-vs-store", "row-hash-disagreement")
    if object_digest(Path(observation.work_dir), sha) == sha:
        state.fail(
            criterion,
            "tamper-not-persisted",
            "tampered object bytes must remain on disk as raw evidence",
        )
    state.op(criterion, approvals, "superseded-record-no-longer-authorizes", "decision-reject")
    state.op(criterion, approvals, "record-chain-verified", "ok")
    if not approvals.field("superseded_record_id"):
        state.fail(criterion, "supersession-link-missing", "second record did not supersede")
    state.op(criterion, approvals, "cas-writer-one-commits", "ok")
    state.op(criterion, approvals, "cas-writer-two-superseded", "superseded")


def check_resume(
    crash_manifest: ControlPlaneFixtureManifest,
    observations: dict[str, GateObservation],
    state: CheckState,
) -> None:
    criterion = "phase-1-cp-idempotent-resume"
    observation = observations.get("cp-crash-before-rename")
    approvals = observations.get("cp-approvals")
    if observation is None or approvals is None:
        state.fail(criterion, "observation-missing", "crash/approvals observation absent")
        return
    sha = _payload_sha(crash_manifest)
    state.op(criterion, observation, "republish", "ok")
    state.op(criterion, observation, "republish-idempotent", "ok")
    state.op(criterion, observation, "reopen", "ok")
    if observation.field("reopen_digest") != sha:
        state.fail(criterion, "resume-reopen-mismatch", "resumed object differs from payload")
    if object_digest(Path(observation.work_dir), sha) != sha:
        state.fail(criterion, "resume-object-drift", "resumed object bytes drift")
    state.op(criterion, approvals, "cas-idempotent-replay", "ok")


__all__ = [
    "ATOMIC",
    "CheckState",
    "GateMismatch",
    "check_atomic",
    "check_bindings",
    "check_crash",
    "check_lease",
    "check_resume",
    "check_stale",
]


__all__ = [
    "check_atomic",
    "check_bindings",
    "check_crash",
    "check_lease",
    "check_resume",
    "check_stale",
]
