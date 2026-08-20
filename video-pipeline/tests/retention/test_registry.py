"""Hash/registry reconciliation: unregistered and drifted paths stay put."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.retention.errors import RetentionError
from services.retention.gc import RetentionGc
from services.retention.models import RetentionPolicy
from tests.retention.support import (
    DAY_SECONDS,
    NOW_EPOCH,
    audit_records,
    policy_payload,
    write_job,
    write_registry,
)

JOB = "job-registry"


def make_gc() -> RetentionGc:
    return RetentionGc(
        policy=RetentionPolicy.model_validate(policy_payload()),
        clock=lambda: NOW_EPOCH,
    )


def test_tampered_registry_entry_retained_and_reported(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    job_dir = write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={"proxies/p1.mp4": b"proxy-bytes", "proxies/p2.mp4": b"proxy-two"},
    )
    honest = {
        "proxies/p1.mp4": hashlib.sha256(b"proxy-bytes").hexdigest(),
        "proxies/p2.mp4": hashlib.sha256(b"WRONG-PAYLOAD").hexdigest(),
    }
    write_registry(job_dir, honest)
    outcome = make_gc().run(root, mode="execute")
    assert not (job_dir / "proxies" / "p1.mp4").exists()
    assert (job_dir / "proxies" / "p2.mp4").is_file(), "hash-mismatch must retain"
    mismatches = [
        record for record in audit_records(root) if record["reason"] == "hash-mismatch"
    ]
    assert mismatches
    assert mismatches[0]["path"] == f"{JOB}/proxies/p2.mp4"
    assert outcome.summary.deleted == 1


def test_unregistered_rebuildable_retained_and_reported(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    job_dir = write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={"proxies/p1.mp4": b"proxy"},
    )
    write_registry(job_dir, {})
    make_gc().run(root, mode="execute")
    assert (job_dir / "proxies" / "p1.mp4").is_file()
    unregistered = [
        record for record in audit_records(root) if record["reason"] == "unregistered"
    ]
    assert unregistered
    assert unregistered[0]["path"] == f"{JOB}/proxies/p1.mp4"


def test_missing_registry_means_every_rebuildable_unregistered(
    tmp_path: Path,
) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    job_dir = write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={"proxies/p1.mp4": b"proxy"},
        with_registry=False,
    )
    outcome = make_gc().run(root, mode="execute")
    assert outcome.summary.deleted == 0
    assert (job_dir / "proxies" / "p1.mp4").is_file()
    reasons = {record["reason"] for record in audit_records(root)}
    assert "unregistered" in reasons


def test_broken_registry_seal_is_typed_error(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    job_dir = write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={"proxies/p1.mp4": b"proxy"},
    )
    tampered = json.loads((job_dir / "retention-registry.json").read_text())
    tampered["entries"]["proxies/p1.mp4"]["sha256"] = "0" * 64
    (job_dir / "retention-registry.json").write_text(json.dumps(tampered))
    with pytest.raises(RetentionError, match="registry-invalid"):
        make_gc().run(root, mode="execute")


def test_disk_drift_after_registry_write_is_retained(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    job_dir = write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={"proxies/p1.mp4": b"proxy"},
    )
    (job_dir / "proxies" / "p1.mp4").write_bytes(b"drifted content")
    make_gc().run(root, mode="execute")
    assert (job_dir / "proxies" / "p1.mp4").is_file()
    reasons = {record["reason"] for record in audit_records(root)}
    assert "hash-mismatch" in reasons
