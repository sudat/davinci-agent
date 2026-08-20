"""Idempotent re-runs: the second pass is a consistent no-op."""

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

JOB = "job-idempotent"


def make_gc() -> RetentionGc:
    return RetentionGc(
        policy=RetentionPolicy.model_validate(policy_payload()),
        clock=lambda: NOW_EPOCH,
    )


def test_second_execute_run_is_a_no_op(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    gc = make_gc()
    first = gc.run(root, mode="execute")
    assert first.summary.deleted > 0
    after_first = snapshot_tree(root / JOB)
    second = gc.run(root, mode="execute")
    assert second.summary.deleted == 0
    assert second.summary.planned_deletes == 0
    assert snapshot_tree(root / JOB) == after_first


def test_audit_remains_consistent_after_rerun(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    gc = make_gc()
    gc.run(root, mode="execute")
    gc.run(root, mode="execute")
    records = audit_records(root)
    deleted_paths = [
        str(record["path"]) for record in records if record["decision"] == "deleted"
    ]
    assert deleted_paths
    for relative in deleted_paths:
        assert not (root / relative).exists()


def test_dry_run_after_execute_plans_nothing(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    gc = make_gc()
    gc.run(root, mode="execute")
    outcome = gc.run(root, mode="dry_run")
    assert outcome.summary.planned_deletes == 0
