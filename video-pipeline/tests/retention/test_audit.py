"""Audit completeness, append-only growth, and disk-truth reflection."""

from __future__ import annotations

from pathlib import Path

from services.retention.audit import DeletionAudit
from services.retention.gc import AUDIT_FILE_NAME, RetentionGc
from services.retention.models import RetentionPolicy
from tests.retention.support import (
    DAY_SECONDS,
    NOW_EPOCH,
    audit_records,
    policy_payload,
    write_job,
)

JOB = "job-audit"


def make_gc() -> RetentionGc:
    return RetentionGc(
        policy=RetentionPolicy.model_validate(policy_payload()),
        clock=lambda: NOW_EPOCH,
    )


def test_every_plan_decision_has_exactly_one_audit_record(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    gc = make_gc()
    outcome = gc.run(root, mode="execute")
    records = audit_records(root)
    assert len(records) == len(outcome.plan.decisions) + outcome.summary.pruned_dirs
    paths = [record["path"] for record in records]
    assert len(paths) == len(set(paths))


def test_deleted_records_reflect_disk_truth(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    make_gc().run(root, mode="execute")
    for record in audit_records(root):
        path = root / str(record["path"])
        if record["decision"] == "deleted" and record["reason"] == "eligible":
            assert not path.exists(), f"audit claims deleted but disk still has {path}"
            assert record["disk_sha256"]


def test_audit_is_append_only_across_runs(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    audit_path = root / AUDIT_FILE_NAME
    gc = make_gc()
    gc.run(root, mode="dry_run")
    first = audit_path.read_bytes()
    gc.run(root, mode="execute")
    second = audit_path.read_bytes()
    assert second.startswith(first)
    DeletionAudit(root).records()


def test_retained_records_carry_reason_and_registry_hash(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={"proxies/p1.mp4": b"proxy", "mystery/x.bin": b"mystery"},
    )
    make_gc().run(root, mode="execute")
    records = audit_records(root)
    retained = [record for record in records if record["decision"] == "retained"]
    assert retained
    assert all(record["reason"] for record in retained)
    deleted = [
        record
        for record in records
        if record["decision"] == "deleted" and record["reason"] == "eligible"
    ]
    assert deleted
    assert all(record["registry_sha256"] for record in deleted)


def test_audit_records_are_strictly_typed(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    make_gc().run(root, mode="execute")
    records = DeletionAudit(root).records()
    assert records
    assert all(record.mode == "execute" for record in records)
    assert all(record.run_id for record in records)
