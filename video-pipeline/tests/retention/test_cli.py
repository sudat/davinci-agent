"""CLI: python -m services.retention.gc (dry-run default, typed nonzero errors)."""

from __future__ import annotations

import json
from pathlib import Path

from services.retention.gc import AUDIT_FILE_NAME, main
from tests.retention.support import (
    DAY_SECONDS,
    NOW_EPOCH,
    audit_records,
    policy_payload,
    write_job,
)

JOB = "job-cli"


def write_policy_file(tmp_path: Path) -> Path:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy_payload()))
    return policy_path


def base_argv(tmp_path: Path, policy_path: Path, *extra: str) -> list[str]:
    return [
        "--jobs-root",
        str(tmp_path / "jobs"),
        "--policy",
        str(policy_path),
        "--now-epoch",
        str(NOW_EPOCH),
        *extra,
    ]


def test_cli_default_is_dry_run_and_writes_audit(tmp_path: Path, capsys) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    policy_path = write_policy_file(tmp_path)
    code = main(base_argv(tmp_path, policy_path))
    assert code == 0
    assert (root / JOB / "proxies" / "p1.mp4").is_file()
    assert (root / AUDIT_FILE_NAME).is_file()
    assert any(record["decision"] == "planned_delete" for record in audit_records(root))
    out = capsys.readouterr().out
    assert "retention-gc mode=dry_run" in out


def test_cli_execute_deletes_and_reports(tmp_path: Path, capsys) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    policy_path = write_policy_file(tmp_path)
    code = main(base_argv(tmp_path, policy_path, "--execute"))
    assert code == 0
    assert not (root / JOB / "proxies" / "p1.mp4").exists()
    out = capsys.readouterr().out
    assert "retention-gc mode=execute" in out
    assert "deleted=5" in out


def test_cli_rejects_conflicting_mode_flags(tmp_path: Path) -> None:
    policy_path = write_policy_file(tmp_path)
    code = main(base_argv(tmp_path, policy_path, "--dry-run", "--execute"))
    assert code == 2


def test_cli_malformed_policy_is_typed_nonzero(tmp_path: Path, capsys) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    bad = tmp_path / "bad-policy.json"
    bad.write_text('{"schema_version": "retention-policy-v1", "rebuildable_retention_days": -5}')
    code = main(base_argv(tmp_path, bad))
    assert code == 2
    assert "policy-invalid" in capsys.readouterr().err


def test_cli_policy_not_json_is_typed_nonzero(tmp_path: Path, capsys) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    bad = tmp_path / "bad-policy.json"
    bad.write_text("not json at all")
    code = main(base_argv(tmp_path, bad))
    assert code == 2
    assert "policy-invalid" in capsys.readouterr().err


def test_cli_tampered_registry_is_typed_nonzero(tmp_path: Path, capsys) -> None:
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
    policy_path = write_policy_file(tmp_path)
    code = main(base_argv(tmp_path, policy_path, "--execute"))
    assert code == 2
    assert "registry-invalid" in capsys.readouterr().err
    assert (job_dir / "proxies" / "p1.mp4").is_file()


def test_cli_malformed_job_state_is_typed_nonzero(tmp_path: Path, capsys) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    job_dir = write_job(
        root,
        JOB,
        status="FROZEN",
        frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS,
        files={"proxies/p1.mp4": b"proxy"},
    )
    (job_dir / "job-state.json").write_text("{not json")
    policy_path = write_policy_file(tmp_path)
    code = main(base_argv(tmp_path, policy_path, "--execute"))
    assert code == 2
    assert "job-state-invalid" in capsys.readouterr().err


def test_cli_missing_jobs_root_is_typed_nonzero(tmp_path: Path, capsys) -> None:
    policy_path = write_policy_file(tmp_path)
    code = main(base_argv(tmp_path, policy_path))
    assert code == 2
    assert "jobs-root-missing" in capsys.readouterr().err


def test_cli_execute_is_required_for_deletion(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    root.mkdir()
    write_job(root, JOB, status="FROZEN", frozen_at_epoch_s=NOW_EPOCH - 100 * DAY_SECONDS)
    policy_path = write_policy_file(tmp_path)
    assert main(base_argv(tmp_path, policy_path, "--dry-run")) == 0
    assert (root / JOB / "proxies" / "p1.mp4").is_file()
