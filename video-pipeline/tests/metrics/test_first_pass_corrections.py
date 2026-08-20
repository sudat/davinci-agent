from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.metrics.bundle import MetricsBundleError, load_bundle
from services.metrics.derive import derive_report
from tests.metrics.support import BundleBuilder, real_approved_episode


def test_first_pass_with_zero_corrections(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    builder.review("ep-real-1", approve_proposal=True, result="v2")
    report = derive_report(load_bundle(builder.write()))

    assert report.first_pass.denominator_count == 1
    assert report.first_pass.first_pass_count == 1
    assert report.first_pass.rate_percent == 100


def test_corrections_drop_first_pass_rate(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    real_approved_episode(builder, "ep-real-2")
    builder.review("ep-real-1", decision_id="dec-1", base="v1", result="v2")
    builder.review("ep-real-1", decision_id="dec-2", base="v2", result="v3")
    report = derive_report(load_bundle(builder.write()))

    assert report.first_pass.first_pass_count == 1
    assert report.first_pass.rate_percent == 50
    assert report.corrections.total_corrections == 2
    assert report.corrections.episodes_with_corrections == 1
    assert report.corrections.max_per_episode == 2


def test_no_review_stream_marks_not_evaluated(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    report = derive_report(load_bundle(builder.write()))

    assert report.first_pass.first_pass_count == 0
    assert report.first_pass.rate_percent is None
    assert report.first_pass.not_evaluated[0].reason == "no review event stream"


def test_tampered_review_chain_rejected(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    builder.review("ep-real-1", decision_id="dec-1", base="v1", result="v2")
    builder.review("ep-real-1", decision_id="dec-2", base="v2", result="v3")
    root = builder.write()
    lines = (root / "review-events.jsonl").read_text().splitlines()
    row = json.loads(lines[1])
    row["event"]["previous_event_hash"] = "f" * 64
    lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    (root / "review-events.jsonl").write_text("\n".join(lines) + "\n")
    with pytest.raises(MetricsBundleError):
        load_bundle(root)
