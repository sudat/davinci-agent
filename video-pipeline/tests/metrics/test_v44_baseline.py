from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.foundation_io import canonical_model_bytes, sha256_file
from services.metrics.v44_baseline import BaselineRecordV1, PytestSummary

BASELINE_SHA = "c37247e9c10fa7c1c2ca50b84f0d1296b50c9b81"
CURRENT_SHA = "a" * 40
MCP_SHA = "b" * 64
V43_EVIDENCE = (
    "capabilities/v4.3/runs/gate-v43-1/gate-summary.json",
    "capabilities/v4.3/runs/probes/episode0-freeze-manifest.json",
)


def _valid_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": "v44-baseline-v1",
        "baseline_commit_sha": BASELINE_SHA,
        "current_commit_sha": CURRENT_SHA,
        "pytest_summary": {"passed": 2993, "failed": 0, "skipped": 2, "tail": "ok tail"},
        "ruff_result": "All checks passed!",
        "basedpyright_result": "0 errors",
        "mcp_fit_sha256": MCP_SHA,
        "v43_gate_evidence": list(V43_EVIDENCE),
        "backends": {"analysis_backend": "hybrid"},
        "created_at": "2026-08-23T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_positive_round_trip(tmp_path: Path) -> None:
    record = BaselineRecordV1.model_validate(_valid_payload())
    # Canonical bytes are stable; validate via file round-trip.
    out = tmp_path / "baseline.json"
    out.write_bytes(canonical_model_bytes(record))
    loaded = BaselineRecordV1.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert loaded == record
    assert loaded.schema_version == "v44-baseline-v1"
    assert sha256_file(out) == sha256_file(out)  # deterministic


def test_canonical_bytes_stable() -> None:
    a = BaselineRecordV1.model_validate(_valid_payload())
    b = BaselineRecordV1.model_validate(_valid_payload())
    assert canonical_model_bytes(a) == canonical_model_bytes(b)


def test_tampered_hash_wrong_length_rejected() -> None:
    with pytest.raises(ValidationError):
        BaselineRecordV1.model_validate(_valid_payload(mcp_fit_sha256="b" * 63))


def test_tampered_hash_non_hex_rejected() -> None:
    with pytest.raises(ValidationError):
        BaselineRecordV1.model_validate(_valid_payload(mcp_fit_sha256="g" * 64))


def test_commit_sha_wrong_length_rejected() -> None:
    with pytest.raises(ValidationError):
        BaselineRecordV1.model_validate(_valid_payload(baseline_commit_sha="abc"))


def test_commit_sha_non_hex_rejected() -> None:
    with pytest.raises(ValidationError):
        BaselineRecordV1.model_validate(_valid_payload(current_commit_sha="z" * 40))


def test_negative_pytest_count_rejected() -> None:
    with pytest.raises(ValidationError):
        PytestSummary(passed=-1, failed=0, skipped=0, tail="x")


def test_negative_failed_rejected() -> None:
    with pytest.raises(ValidationError):
        BaselineRecordV1.model_validate(
            _valid_payload(pytest_summary={"passed": 0, "failed": -1, "skipped": 0, "tail": "x"})
        )


def test_missing_required_field_rejected() -> None:
    payload = _valid_payload()
    del payload["mcp_fit_sha256"]
    with pytest.raises(ValidationError):
        BaselineRecordV1.model_validate(payload)


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        BaselineRecordV1.model_validate(_valid_payload(unexpected="field"))  # type: ignore[arg-type]


def test_cli_write_round_trip(tmp_path: Path) -> None:
    pytest_tail = tmp_path / "pytest_tail.txt"
    pytest_tail.write_text("2993 passed", encoding="utf-8")
    out = tmp_path / "baseline.json"
    # Run CLI as subprocess from video-pipeline (mirrors MUST DO #6).
    video_pipeline = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [  # test harness invoking own CLI
            "uv",
            "run",
            "python",
            "-m",
            "services.cli.v44_baseline",
            "write",
            "--out",
            str(out),
            "--pytest",
            "passed=10,failed=0,skipped=1",
            "--pytest-tail-file",
            str(pytest_tail),
            "--ruff",
            "All checks passed!",
            "--basedpyright",
            "0 errors",
        ],
        cwd=video_pipeline,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = BaselineRecordV1.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert loaded.pytest_summary.passed == 10
    assert loaded.pytest_summary.tail == "2993 passed"
    assert loaded.ruff_result == "All checks passed!"
    assert loaded.mcp_fit_sha256 == sha256_file(
        video_pipeline / "capabilities" / "v4.4" / "mcp-fit.json"
    )
