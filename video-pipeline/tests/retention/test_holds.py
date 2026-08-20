"""Investigation holds: ACTIVE/NEEDS_HUMAN/FAILED/missing state block deletion."""

from __future__ import annotations

from pathlib import Path

from services.retention.gc import RetentionGc
from services.retention.models import RetentionPolicy
from tests.retention.support import (
    DAY_SECONDS,
    NOW_EPOCH,
    audit_records,
    policy_payload,
    write_job,
)

EXPECTED_SURVIVORS = (
    "proxies/p1.mp4",
    "proxies/nested/p2.mp4",
    "previews/editorial.mp4",
    "analysis/frames/f001.jpg",
    "cache/asr/k1/transcript.json",
)


def make_gc() -> RetentionGc:
    return RetentionGc(
        policy=RetentionPolicy.model_validate(policy_payload()),
        clock=lambda: NOW_EPOCH,
    )


def assert_sweepables_untouched(root: Path, job_id: str) -> None:
    for relative in EXPECTED_SURVIVORS:
        path = root / job_id / relative
        assert path.is_file(), f"held rebuildable was deleted: {relative}"


def test_active_job_holds_block_rebuildable_deletion(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, "job-active", status="ACTIVE")
    outcome = make_gc().run(root, mode="execute")
    assert outcome.summary.deleted == 0
    assert outcome.summary.retained > 0
    assert_sweepables_untouched(root, "job-active")
    reasons = {record["reason"] for record in audit_records(root)}
    assert "hold-active" in reasons


def test_needs_human_job_holds_block_rebuildable_deletion(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, "job-human", status="NEEDS_HUMAN")
    outcome = make_gc().run(root, mode="execute")
    assert outcome.summary.deleted == 0
    assert_sweepables_untouched(root, "job-human")
    reasons = {record["reason"] for record in audit_records(root)}
    assert "hold-needs-human" in reasons


def test_failed_job_holds_block_rebuildable_deletion(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, "job-failed", status="FAILED")
    outcome = make_gc().run(root, mode="execute")
    assert outcome.summary.deleted == 0
    assert_sweepables_untouched(root, "job-failed")
    reasons = {record["reason"] for record in audit_records(root)}
    assert "hold-failed" in reasons


def test_missing_state_file_holds_whole_job(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, "job-unknown", with_state=False)
    outcome = make_gc().run(root, mode="execute")
    assert outcome.summary.deleted == 0
    assert_sweepables_untouched(root, "job-unknown")
    reasons = {record["reason"] for record in audit_records(root)}
    assert "hold-missing-state" in reasons


def test_hold_applies_to_runtime_cache_too(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, "job-active", status="ACTIVE")
    make_gc().run(root, mode="execute")
    cache_records = [
        record
        for record in audit_records(root)
        if record["path"] == "job-active/cache" and record["decision"] == "retained"
    ]
    assert cache_records
    assert cache_records[0]["reason"] == "hold-active"


def test_frozen_but_period_not_elapsed_retains(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(
        root, "job-young", status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 10 * DAY_SECONDS
    )
    outcome = make_gc().run(root, mode="execute")
    assert outcome.summary.deleted == 0
    assert_sweepables_untouched(root, "job-young")
    reasons = {record["reason"] for record in audit_records(root)}
    assert "retention-period" in reasons


def test_period_boundary_day_exactly_elapsed_is_eligible(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(
        root, "job-edge", status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 30 * DAY_SECONDS
    )
    outcome = make_gc().run(root, mode="execute")
    assert outcome.summary.deleted > 0
