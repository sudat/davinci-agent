"""Attack class 7 extension (F3): untrusted file content never reaches logs.

A poisoned ``job-state.json`` carrying marker secrets must produce a
TYPED ``job-state-invalid`` failure whose detail carries field/type
information only — never the raw file bytes — through the library
error, the GC CLI stderr/stdout, and any audit output.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from services.retention.errors import RetentionError
from services.retention.walk import load_job_state, load_registry

MARKER_SECRET = "sk-live-f3-marker-9d2e"  # noqa: S105 (leak-canary payload, not a credential)
MARKER_PII = "operator-f3@example.invalid"

POISONED_STATE = {
    "schema_version": "retention-job-state-v1",
    "job_id": "job-poison",
    "status": "FROZEN",
    "frozen_at_epoch_s": 1_000_000_000,
    "secret_note": MARKER_SECRET,
    "contact": MARKER_PII,
}


def _poisoned_job(jobs: Path) -> Path:
    job = jobs / "job-poison"
    job.mkdir(parents=True)
    (job / "sources").mkdir()
    (job / "sources" / "cam.mov").write_bytes(b"camera-original")
    (job / "job-state.json").write_text(json.dumps(POISONED_STATE), encoding="utf-8")
    return job


def _policy_file(tmp_path: Path) -> Path:
    payload = {
        "schema_version": "retention-policy-v1",
        "rebuildable_retention_days": 30,
        "authoritative_names": ("sources", "artifacts", "renders"),
        "manual_finalization_names": ("manual-finalization",),
        "rebuildable_names": ("proxies", "previews", "analysis", "contacts"),
        "runtime_cache_names": ("cache",),
    }
    path = tmp_path / "retention-policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_10_poisoned_job_state_error_detail_carries_no_file_content(
    tmp_path: Path,
) -> None:
    job = _poisoned_job(tmp_path / "jobs")
    with_raises = None
    try:
        load_job_state(job)
    except RetentionError as error:
        with_raises = error
    assert with_raises is not None
    assert with_raises.code == "job-state-invalid"
    assert MARKER_SECRET not in with_raises.detail
    assert MARKER_PII not in with_raises.detail
    assert "secret_note" in with_raises.detail  # field-level info survives


def test_11_poisoned_registry_error_detail_carries_no_file_content(
    tmp_path: Path,
) -> None:
    job = tmp_path / "jobs" / "job-r"
    job.mkdir(parents=True)
    (job / "retention-registry.json").write_text(
        json.dumps({"schema_version": "retention-registry-v1", "leak": MARKER_SECRET}),
        encoding="utf-8",
    )
    error: RetentionError | None = None
    try:
        load_registry(job)
    except RetentionError as caught:
        error = caught
    assert error is not None
    assert error.code == "registry-invalid"
    assert MARKER_SECRET not in error.detail


def test_20_gc_cli_never_echoes_poisoned_job_state(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    _poisoned_job(jobs)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.retention.gc",
            "--jobs-root",
            str(jobs),
            "--policy",
            str(_policy_file(tmp_path)),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path.cwd(),
        timeout=60,
    )
    assert result.returncode != 0
    assert "job-state-invalid" in result.stderr or "job-state-invalid" in result.stdout
    combined = result.stdout + result.stderr
    assert MARKER_SECRET not in combined
    assert MARKER_PII not in combined
    for path in jobs.rglob("*"):
        if path.is_file() and "job-state.json" not in path.name:
            assert MARKER_SECRET not in path.read_text(encoding="utf-8", errors="replace")
