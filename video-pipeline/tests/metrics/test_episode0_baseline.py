"""Episode-0 longitudinal baseline tooling — TDD (failing first).

Covers: manifest freeze (deterministic sha/size, never fabricate duration),
report generation (stable canonical bytes), missing-field ValidationError,
compare delta.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError


def _sample_log_dict() -> dict[str, object]:
    return {
        "schema_version": "episode0-baseline-v1",
        "active_human_time_minutes": 42.5,
        "ttfrp_minutes": 10.0,
        "wall_clock_minutes": 60.0,
        "manual_resolve_minutes": 5.0,
        "wrong_keep_remove": [{"ts": "00:01:23", "note": "kept filler"}],
        "missed_moments": [{"ts": "00:02:00", "note": "missed reaction"}],
        "finishing_deficits": [{"ts": "00:03:00", "note": "color off"}],
        "interruption_points": [{"ts": "00:04:00", "note": "interrupted"}],
        "publishability": {
            "publishable": False,
            "comment": "needs finishing",
            "best_ts": "00:01:00",
            "worst_ts": "00:03:00",
        },
    }


def test_manifest_freeze_deterministic(tmp_path: Path) -> None:
    """Given: tmp fake video file; When: compute manifest twice; Then: sha/size stable, duration not fabricated."""
    from services.metrics.episode0_baseline import compute_source_manifest

    fake = tmp_path / "fake.mp4"
    content = b"fake video content 123"
    fake.write_bytes(content)
    expected_sha = hashlib.sha256(content).hexdigest()
    expected_size = len(content)

    manifest = compute_source_manifest(fake)
    assert manifest.sha256 == expected_sha
    assert manifest.size_bytes == expected_size
    # duration must be either probed float or None with flag False — never fabricated
    if manifest.duration_probed:
        assert manifest.duration_seconds is not None
        assert isinstance(manifest.duration_seconds, float)
        assert manifest.duration_seconds >= 0
    else:
        assert manifest.duration_seconds is None

    second = compute_source_manifest(fake)
    assert second.sha256 == manifest.sha256
    assert second.size_bytes == manifest.size_bytes
    assert second.duration_probed == manifest.duration_probed
    assert second.duration_seconds == manifest.duration_seconds


def test_full_log_to_report_stable_canonical_bytes(tmp_path: Path) -> None:
    """Given: full log + manifest; When: report generated twice; Then: stable canonical bytes on disk."""
    from services.metrics.episode0_baseline import (
        Episode0BaselineLogV1,
        compute_source_manifest,
        generate_report,
    )

    fake = tmp_path / "fake2.mp4"
    fake.write_bytes(b"hello world baseline")
    manifest = compute_source_manifest(fake)
    log = Episode0BaselineLogV1.model_validate(_sample_log_dict())

    runs_root = tmp_path / "runs"
    first_path = generate_report(log, manifest, run_id="baseline", runs_root=runs_root)
    assert first_path.is_file()
    assert first_path == runs_root / "baseline" / "report.json"
    first_bytes = first_path.read_bytes()
    first_json: object = json.loads(first_bytes)
    assert isinstance(first_json, dict)
    # stable keys via canonical: sort_keys separators
    canonical = json.dumps(first_json, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert first_bytes == canonical or first_bytes == canonical + b"\n" or first_bytes.strip() == canonical

    # second invocation must produce identical bytes (stale_state probe)
    second_path = generate_report(log, manifest, run_id="baseline", runs_root=runs_root)
    assert second_path.read_bytes() == first_bytes


def test_missing_required_field_raises_validation_error() -> None:
    """Given: log dict missing a required field; When: validated; Then: ValidationError."""
    from services.metrics.episode0_baseline import Episode0BaselineLogV1

    bad = _sample_log_dict()
    bad.pop("active_human_time_minutes")
    with pytest.raises(ValidationError) as exc:
        Episode0BaselineLogV1.model_validate(bad)
    assert "active_human_time_minutes" in str(exc.value)


def test_compare_emits_delta(tmp_path: Path) -> None:
    """Given: two reports with differing timing; When: compared; Then: delta summary names the change."""
    from services.metrics.episode0_baseline import (
        Episode0BaselineLogV1,
        compare_reports,
        compute_source_manifest,
        generate_report,
    )

    fake = tmp_path / "fake3.mp4"
    fake.write_bytes(b"compare content")
    manifest = compute_source_manifest(fake)

    log_a = Episode0BaselineLogV1.model_validate({**_sample_log_dict(), "active_human_time_minutes": 30.0})
    log_b = Episode0BaselineLogV1.model_validate({**_sample_log_dict(), "active_human_time_minutes": 45.0})

    report_a = generate_report(log_a, manifest, run_id="run-a", runs_root=tmp_path / "runs")
    report_b = generate_report(log_b, manifest, run_id="run-b", runs_root=tmp_path / "runs")

    delta = compare_reports(report_a, report_b)
    assert isinstance(delta, dict)
    # must mention active_human_time in some delta key/value
    joined = json.dumps(delta, sort_keys=True)
    assert "active_human_time_minutes" in joined
    # numeric delta should be 15.0 (b - a)
    assert "15" in joined or 15 in str(delta.values()) or 15.0 in str(delta.values())


def test_cli_help_smoke() -> None:
    """CLI --help lists subcommands."""
    result = subprocess.run(
        [sys.executable, "-m", "services.cli.episode0", "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path.cwd(),
        timeout=30,
    )
    assert result.returncode == 0
    assert "freeze-manifest" in result.stdout
    assert "report" in result.stdout
    assert "compare" in result.stdout
