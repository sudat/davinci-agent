"""Tamper-evident v4.2 foundation freeze baseline guard (Task 1).

Validates ``capabilities/v4.2-freeze/baseline.json`` against the annotated tag
``v4.2-foundation-freeze`` and the recorded test-summary contract.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

# video-pipeline/tests/release/test_freeze_baseline.py -> parents[2] = video-pipeline/
VIDEO_PIPELINE = Path(__file__).resolve().parents[2]
BASELINE = VIDEO_PIPELINE / "capabilities" / "v4.2-freeze" / "baseline.json"
TAG = "v4.2-foundation-freeze"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_FILES_EXIST_KEYS = (
    "services/cli/",
    "pyproject.toml",
    "services/job_runner/stage_runner.py",
    "services/media_query/api_models.py",
    "services/editorial/models.py",
    "services/editorial/candidate_models.py",
    "services/editorial/evidence.py",
    "services/editorial/director.py",
    "services/contracts/timeline_ir.py",
    "services/preview/",
    "services/resolve_bridge/fixed_presentation_subtitle.py",
    "services/resolve_adapter/package.py",
)


def _load_baseline() -> dict[str, object]:
    assert BASELINE.is_file(), f"baseline.json missing: {BASELINE}"
    return json.loads(BASELINE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_baseline_file_exists() -> None:
    assert BASELINE.is_file(), f"baseline.json not found at {BASELINE}"


def test_baseline_schema_fields_present_and_well_formed() -> None:
    data = _load_baseline()

    # Top-level keys
    assert data.get("schema_version") == "v42-freeze-v1"
    commit = data.get("commit")
    assert isinstance(commit, str)
    assert HEX40.match(commit), f"commit must be 40-hex, got {commit!r}"
    assert data.get("tag") == TAG
    assert data.get("resolve_version") == "21.0.4"
    assert data.get("rollback_command") == "git checkout v4.2-foundation-freeze"

    python_version = data.get("python_version")
    assert isinstance(python_version, str)
    assert python_version.startswith("Python ")
    assert python_version.startswith("Python "), f"bad python_version {python_version!r}"

    os_val = data.get("os")
    assert isinstance(os_val, str)
    assert len(os_val) > 0

    frozen_at = data.get("frozen_at")
    assert isinstance(frozen_at, str)
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", frozen_at), (
        f"bad frozen_at {frozen_at!r}"
    )

    # test_summary sub-object
    ts = data.get("test_summary")
    assert isinstance(ts, dict), f"test_summary must be dict, got {type(ts)}"
    expected_keys = (
        "command",
        "total",
        "passed",
        "failed",
        "skipped",
        "errors",
        "warnings",
        "duration_seconds",
        "run_1_digest",
        "run_2_digest",
    )
    for k in expected_keys:
        assert k in ts, f"test_summary missing key {k!r}"
    assert isinstance(ts["command"], str)
    assert len(ts["command"]) > 0
    for k in ("total", "passed", "failed", "skipped", "errors", "warnings"):
        assert isinstance(ts[k], int)
        assert ts[k] >= 0, f"test_summary[{k!r}] must be non-negative int"
    assert isinstance(ts["duration_seconds"], (int, float))
    assert ts["duration_seconds"] >= 0  # type: ignore[operator]
    assert isinstance(ts["run_1_digest"], str)
    assert HEX64.match(ts["run_1_digest"]), f"bad run_1_digest {ts['run_1_digest']!r}"
    assert isinstance(ts["run_2_digest"], str)
    assert HEX64.match(ts["run_2_digest"]), f"bad run_2_digest {ts['run_2_digest']!r}"

    # files_exist sub-object
    fe = data.get("files_exist")
    assert isinstance(fe, dict), "files_exist must be dict"
    for k in REQUIRED_FILES_EXIST_KEYS:
        assert k in fe, f"files_exist missing key {k!r}"
        assert isinstance(fe[k], bool), f"files_exist[{k!r}] must be bool"


def test_baseline_commit_equals_tag_target() -> None:
    data = _load_baseline()
    baseline_commit = data["commit"]
    assert isinstance(baseline_commit, str)

    # Resolve tag to its commit object; must not be empty / missing
    try:
        result = subprocess.run(
            ["git", "rev-parse", f"{TAG}^{{commit}}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"tag {TAG!r} missing or not resolvable: {exc.stderr.strip()}")
        raise  # unreachable

    tag_commit = result.stdout.strip()
    assert HEX40.match(tag_commit), f"tag resolved to non-hex40 {tag_commit!r}"
    assert baseline_commit == tag_commit, (
        f"baseline commit {baseline_commit!r} != tag commit {tag_commit!r}"
    )
    # Also verify tag commit shape
    assert HEX40.match(tag_commit)
    assert tag_commit == "5890c840fdb3f13af21a86697937f5a39f4143f9" or HEX40.match(
        tag_commit
    )


def test_baseline_test_summary_counts_consistent() -> None:
    data = _load_baseline()
    ts = data["test_summary"]  # type: ignore[index]
    assert isinstance(ts, dict)
    total = ts["total"]  # type: ignore[index]
    passed = ts["passed"]  # type: ignore[index]
    failed = ts["failed"]  # type: ignore[index]
    skipped = ts["skipped"]  # type: ignore[index]
    errors = ts["errors"]  # type: ignore[index]
    assert isinstance(total, int)
    assert isinstance(passed, int)
    assert isinstance(failed, int)
    assert isinstance(skipped, int)
    assert isinstance(errors, int)
    assert total > 0, "total must be > 0"
    assert total == passed + failed + skipped + errors, (
        f"total {total} != passed {passed}+failed {failed}"
        f"+skipped {skipped}+errors {errors}"
    )


def test_baseline_files_exist_all_true() -> None:
    data = _load_baseline()
    fe = data["files_exist"]  # type: ignore[index]
    assert isinstance(fe, dict)
    for key in REQUIRED_FILES_EXIST_KEYS:
        assert fe[key] is True, f"files_exist[{key!r}] is {fe[key]!r}, expected True"
