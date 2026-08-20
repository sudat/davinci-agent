"""Happy path: verified-freeze eligibility, preservation, dry-run, drift."""

from __future__ import annotations

from pathlib import Path

from services.retention.gc import RetentionGc
from services.retention.models import RetentionPolicy
from tests.retention.support import (
    DAY_SECONDS,
    NOW_EPOCH,
    audit_records,
    policy_payload,
    snapshot_tree,
    write_job,
)

JOB = "job-happy"


def make_gc() -> RetentionGc:
    return RetentionGc(
        policy=RetentionPolicy.model_validate(policy_payload()),
        clock=lambda: NOW_EPOCH,
    )


def test_frozen_job_rebuildables_deleted_after_retention_period(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    outcome = make_gc().run(root, mode="execute")
    assert outcome.summary.deleted == 5
    for relative in (
        f"{JOB}/proxies/p1.mp4",
        f"{JOB}/proxies/nested/p2.mp4",
        f"{JOB}/previews/editorial.mp4",
        f"{JOB}/analysis/frames/f001.jpg",
        f"{JOB}/cache/asr/k1/transcript.json",
    ):
        assert not (root / relative).exists(), f"eligible rebuildable survived: {relative}"


def test_authoritative_and_manual_finalization_always_retained(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    make_gc().run(root, mode="execute")
    for relative in (
        f"{JOB}/sources/cam-a.mov",
        f"{JOB}/artifacts/selection-plan.json",
        f"{JOB}/artifacts/edit-plan.json",
        f"{JOB}/renders/final.mp4",
        f"{JOB}/manual-finalization/frozen.json",
        f"{JOB}/job-state.json",
        f"{JOB}/retention-registry.json",
    ):
        assert (root / relative).is_file(), f"protected path deleted: {relative}"
    reasons = {
        (record["path"], record["reason"]) for record in audit_records(root)
    }
    assert (f"{JOB}/sources", "authoritative") in reasons
    assert (f"{JOB}/manual-finalization", "manual-finalization") in reasons
    assert (f"{JOB}/job-state.json", "protected") in reasons


def test_dry_run_is_default_and_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    gc = make_gc()
    job_tree = root / JOB
    before = snapshot_tree(job_tree)
    outcome = gc.run(root, mode="dry_run")
    assert snapshot_tree(job_tree) == before
    assert outcome.summary.deleted == 0
    assert outcome.summary.planned_deletes == 5
    dry_records = audit_records(root)
    assert all(
        record["decision"] in ("planned_delete", "retained") for record in dry_records
    )
    assert any(record["decision"] == "planned_delete" for record in dry_records)


def test_execute_rejects_drift_between_plan_and_execute(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    gc = make_gc()
    plan = gc.plan(root)
    victim = root / JOB / "proxies" / "p1.mp4"
    victim.write_bytes(b"mutated after plan")
    outcome = gc.execute(root, plan)
    assert victim.is_file(), "drifted file must be retained, not deleted"
    drift = [
        record
        for record in audit_records(root)
        if record["reason"] == "drift-before-execute"
    ]
    assert drift
    assert drift[0]["path"] == f"{JOB}/proxies/p1.mp4"
    assert outcome.summary.deleted == 4


def test_execute_only_touches_planned_paths(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    gc = make_gc()
    plan = gc.plan(root)
    planned = {
        decision.path.removeprefix(f"{JOB}/")
        for decision in plan.decisions
        if decision.action == "planned_delete"
    }
    before = snapshot_tree(root / JOB)
    gc.execute(root, plan)
    after = snapshot_tree(root / JOB)
    removed_files = {
        relative
        for relative in set(before) - set(after)
        if str(before[relative]).startswith("file:")
    }
    removed_dirs = set(before) - set(after) - removed_files
    assert removed_files <= planned
    assert all(str(before[relative]) == "dir" for relative in removed_dirs)


def test_unclassified_entries_are_retained_and_reported(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={
            "proxies/p1.mp4": b"proxy",
            "mystery-dir/thing.bin": b"unknown class",
        },
    )
    make_gc().run(root, mode="execute")
    assert (root / JOB / "mystery-dir" / "thing.bin").is_file()
    reasons = {(record["path"], record["reason"]) for record in audit_records(root)}
    assert (f"{JOB}/mystery-dir", "unclassified") in reasons
