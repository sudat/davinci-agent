from __future__ import annotations

import json
from pathlib import Path

from services.metrics.report import main
from tests.metrics.support import BundleBuilder, full_phase_timing


def _technical_bundle(root: Path) -> Path:
    builder = BundleBuilder(root)
    builder.episode("ep-tech-1", kind="technical")
    builder.episode("ep-syn-1", kind="synthetic")
    builder.review("ep-tech-1", decision_id="dec-1", base="v1", result="v2")
    builder.stage("ep-tech-1", "build", "attempt-failed", attempt=1)
    builder.stage("ep-tech-1", "build", "attempt-succeeded", attempt=2)
    builder.qc_outcome("ep-tech-1", verdict="passed")
    builder.qc_outcome("ep-syn-1", verdict="blocked", blockers=1)
    builder.artifact("ep-tech-1", "art-1", "authoritative", 1000)
    builder.artifact("ep-syn-1", "art-2", "rebuildable", 500)
    builder.trace_source("ep-tech-1", "src-1")
    builder.trace_decision("ep-tech-1", "dec-1", ("src-1",))
    builder.trace_item("ep-tech-1", "item-1", "dec-1")
    return builder.write()


def test_cli_complete_technical_bundle_valid_and_labeled(tmp_path: Path) -> None:
    events = _technical_bundle(tmp_path / "bundle")
    out = tmp_path / "report.json"
    assert main(["--events", str(events), "--out", str(out)]) == 0

    payload = json.loads(out.read_text())
    assert payload["schema_version"] == "metrics-report-v1"
    assert payload["validation"]["valid"] is True
    assert payload["eligibility"]["technical_episode_ids"] == ["ep-tech-1"]
    assert payload["eligibility"]["synthetic_episode_ids"] == ["ep-syn-1"]
    assert payload["eligibility"]["denominator_count"] == 0
    for metric in payload["time_metrics"]:
        assert metric["sample_count"] == 0
        assert metric["median_ms"] is None
        assert metric["p90_ms"] is None
    assert payload["trace"]["status"] == "complete"
    bindings = {entry["name"]: entry["sha256"] for entry in payload["input_bindings"]}
    assert set(bindings) >= {"episodes.json", "review-events.jsonl", "stage-events.jsonl"}
    assert bindings["episodes.json"] is not None
    assert bindings["timing-events.jsonl"] is None


def test_cli_unsupported_kpi_claim_exits_nonzero(tmp_path: Path, capsys) -> None:
    events = _technical_bundle(tmp_path / "bundle")
    claim = {
        "schema_version": "metrics-claims-v1",
        "claims": [
            {
                "claim_id": "claim-30min",
                "metric": "active_human_time_median_ms",
                "value": 1_800_000,
            }
        ],
    }
    (events / "claims.json").write_text(json.dumps(claim))
    out = tmp_path / "report.json"
    assert main(["--events", str(events), "--out", str(out)]) == 1

    captured = capsys.readouterr()
    assert "kpi_claim_unsupported" in captured.err
    payload = json.loads(out.read_text())
    assert payload["validation"]["valid"] is False
    assert payload["validation"]["failures"][0]["code"] == "kpi_claim_unsupported"


def test_cli_malformed_bundle_exits_two(tmp_path: Path, capsys) -> None:
    events = _technical_bundle(tmp_path / "bundle")
    (events / "episodes.json").write_text("{not json")
    out = tmp_path / "report.json"
    assert main(["--events", str(events), "--out", str(out)]) == 2
    assert not out.exists()


def test_cli_false_real_approval_exits_nonzero(tmp_path: Path, capsys) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-real-1")
    full_phase_timing(builder, "ep-real-1")
    out = tmp_path / "report.json"
    assert main(["--events", str(builder.write()), "--out", str(out)]) == 1
    assert "false_real_approval" in capsys.readouterr().err


def test_cli_hidden_episode_exits_nonzero(tmp_path: Path, capsys) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-real-1")
    builder.timing("ep-ghost", "review", "start", 1700000000)
    out = tmp_path / "report.json"
    assert main(["--events", str(builder.write()), "--out", str(out)]) == 1
    assert "hidden_out_of_contract" in capsys.readouterr().err


def test_cli_broken_trace_exits_nonzero(tmp_path: Path, capsys) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    builder.trace_decision("ep-tech-1", "dec-1", ())
    builder.trace_item("ep-tech-1", "item-1", "dec-1")
    out = tmp_path / "report.json"
    assert main(["--events", str(builder.write()), "--out", str(out)]) == 1
    assert "broken_trace" in capsys.readouterr().err
