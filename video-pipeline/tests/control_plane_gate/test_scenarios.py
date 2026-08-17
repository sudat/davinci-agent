from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.job_runner.gate_cp_models import GateObservation
from services.job_runner.gate_cp_scenarios import (
    MANIFEST_DIR,
    drive_scenarios,
    load_manifests,
)

if TYPE_CHECKING:
    from services.fixtures.manifest_control_plane import ControlPlaneFixtureManifest


def driven(tmp_path: Path, fixture_id: str) -> GateObservation:
    evidence = tmp_path / "evidence"
    drive_scenarios(evidence, MANIFEST_DIR)
    raw = (evidence / "scenarios" / fixture_id / "observation.json").read_bytes()
    return GateObservation.model_validate_json(raw)


def manifests() -> dict[str, ControlPlaneFixtureManifest]:
    return load_manifests(MANIFEST_DIR)


def test_cp_atomic_publish_scenario_recomputes(tmp_path: Path) -> None:
    observation = driven(tmp_path, "cp-atomic-publish")
    manifest = manifests()["cp-atomic-publish"]
    assert observation.operation("publish").result == "ok"
    assert observation.operation("reopen").result == "ok"
    assert observation.field("reopen_digest") == manifest.scenario.payload.sha256
    object_file = (
        Path(observation.work_dir)
        / "store"
        / "objects"
        / manifest.scenario.payload.sha256[:2]
        / manifest.scenario.payload.sha256
    )
    assert object_file.is_file()


def test_cp_crash_before_rename_scenario_recomputes(tmp_path: Path) -> None:
    observation = driven(tmp_path, "cp-crash-before-rename")
    assert observation.operation("reconcile").result == "discard-crashed-temp"
    assert observation.operation("republish").result == "ok"
    assert observation.operation("republish-idempotent").result == "ok"
    assert observation.operation("reopen").result == "ok"


def test_cp_orphan_reconcile_scenario_recomputes(tmp_path: Path) -> None:
    observation = driven(tmp_path, "cp-orphan-reconcile")
    assert observation.operation("reconcile").result == "discard-orphan-temp"
    assert observation.operation("registry-reconcile").result == "adopted=1"
    assert observation.field("registry_adopted") == "true"
    manifest = manifests()["cp-orphan-reconcile"]
    sha = manifest.scenario.payload.sha256
    work = Path(observation.work_dir)
    assert not (work / "store" / "objects" / sha[:2] / sha).exists()
    assert not (work / "store" / "objects" / sha[:2] / f".obj-{sha}.tmp").exists()


def test_cp_stale_cas_state_artifact_inconsistency_fails_closed(tmp_path: Path) -> None:
    observation = driven(tmp_path, "cp-stale-cas")
    assert observation.operation("reopen").result == "content-hash-mismatch"
    assert observation.operation("state-verify-vs-store").result == "row-hash-disagreement"


def test_cp_lease_expiry_scenario_recomputes(tmp_path: Path) -> None:
    observation = driven(tmp_path, "cp-lease-expiry")
    assert observation.operation("expired-commit").result == "lease-expired"
    assert observation.operation("new-holder-acquire").result == "ok"
    assert observation.operation("new-holder-commit").result == "ok"
    assert observation.operation("lane-holder-a-apply").result == "ok"
    assert observation.operation("lane-expired-holder-refused").result == "lease-expired"
    assert observation.operation("lane-new-holder-commit").result == "ok"


def test_cp_path_symlink_denial_scenario_recomputes(tmp_path: Path) -> None:
    observation = driven(tmp_path, "cp-path-symlink-denial")
    assert observation.operation("publish").result == "symlinked-path-component"
    manifest = manifests()["cp-path-symlink-denial"]
    sha = manifest.scenario.payload.sha256
    object_file = (
        Path(observation.work_dir) / "store" / "objects" / sha[:2] / sha
    )
    assert not object_file.exists()


def test_scenario_rerun_is_idempotent(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    first = drive_scenarios(evidence, MANIFEST_DIR)
    second = drive_scenarios(evidence, MANIFEST_DIR)
    assert set(first) == set(second)
    assert all(path.is_file() for path in second.values())
