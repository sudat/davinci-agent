"""Traversal safety: no-follow, no escape, cycle-proof."""

from __future__ import annotations

import os
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

JOB = "job-traversal"


def make_gc() -> RetentionGc:
    return RetentionGc(
        policy=RetentionPolicy.model_validate(policy_payload()),
        clock=lambda: NOW_EPOCH,
    )


def test_symlink_pointing_outside_retained_and_reported(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_bytes(b"must never be deleted")
    job_dir = write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={"proxies/p1.mp4": b"proxy"},
    )
    (job_dir / "proxies" / "escape.txt").symlink_to(secret)
    outcome = make_gc().run(root, mode="execute")
    assert secret.read_bytes() == b"must never be deleted"
    assert (job_dir / "proxies" / "escape.txt").is_symlink()
    records = [
        record
        for record in audit_records(root)
        if record["path"] == f"{JOB}/proxies/escape.txt"
    ]
    assert records
    record = records[0]
    assert record["decision"] == "retained"
    assert record["reason"] == "symlink-escape"
    assert record["symlink_target"] == str(secret)
    assert outcome.summary.deleted > 0


def test_symlink_dir_inside_tree_is_never_followed(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    job_dir = write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={"proxies/p1.mp4": b"proxy", "sources/real.mov": b"source"},
    )
    (job_dir / "proxies" / "link-to-sources").symlink_to(job_dir / "sources")
    make_gc().run(root, mode="execute")
    assert (job_dir / "sources" / "real.mov").is_file()
    assert (job_dir / "proxies" / "link-to-sources").is_symlink()
    records = [
        record
        for record in audit_records(root)
        if record["path"] == f"{JOB}/proxies/link-to-sources"
    ]
    assert records
    assert records[0]["reason"] in ("symlink", "symlink-escape")


def test_symlink_cycle_terminates(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    job_dir = write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={"proxies/p1.mp4": b"proxy"},
    )
    (job_dir / "proxies" / "loop-a").symlink_to(job_dir / "proxies")
    outcome = make_gc().run(root, mode="execute")
    assert outcome.summary.deleted > 0
    assert (job_dir / "proxies" / "loop-a").is_symlink()


def test_every_touched_path_stays_under_managed_root(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "bait.txt").write_bytes(b"bait")
    make_gc().run(root, mode="execute")
    assert (outside / "bait.txt").is_file()
    real_root = os.path.realpath(root)
    for record in audit_records(root):
        candidate = os.path.realpath(root / str(record["path"]))
        assert candidate.startswith(real_root + os.sep) or candidate == real_root


def test_symlinked_classified_top_name_is_not_descended(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    outside = tmp_path / "outside-bin"
    outside.mkdir()
    (outside / "p2.mp4").write_bytes(b"outside proxy")
    job_dir = write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={"proxies/p1.mp4": b"proxy"},
    )
    (job_dir / "previews").symlink_to(outside)
    before_outside = snapshot_tree(outside)
    make_gc().run(root, mode="execute")
    assert snapshot_tree(outside) == before_outside
    records = [
        record for record in audit_records(root) if record["path"] == f"{JOB}/previews"
    ]
    assert records
    assert records[0]["reason"] == "symlink-escape"
