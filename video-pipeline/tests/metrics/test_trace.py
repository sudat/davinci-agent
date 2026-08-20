from __future__ import annotations

from pathlib import Path

from services.metrics.bundle import load_bundle
from services.metrics.derive import derive_report
from services.metrics.validate import validate_report
from tests.metrics.support import BundleBuilder


def _complete_trace(builder: BundleBuilder) -> None:
    builder.trace_source("ep-tech-1", "src-1")
    builder.trace_source("ep-tech-1", "src-2")
    builder.trace_decision("ep-tech-1", "dec-1", ("src-1",))
    builder.trace_decision("ep-tech-1", "dec-2", ("src-2",))
    builder.trace_item("ep-tech-1", "item-1", "dec-1")
    builder.trace_item("ep-tech-1", "item-2", "dec-2")


def test_complete_trace(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    _complete_trace(builder)
    builder.review("ep-tech-1", decision_id="dec-1", base="v1", result="v2")
    report = derive_report(load_bundle(builder.write()))

    assert report.trace.status == "complete"
    assert report.trace.decision_count == 2
    assert report.trace.build_item_count == 2
    assert report.trace.review_linked_decisions == 1
    assert report.trace.breaks == ()


def test_decision_without_source_span_broken(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    builder.trace_decision("ep-tech-1", "dec-1", ())
    builder.trace_item("ep-tech-1", "item-1", "dec-1")
    report = derive_report(load_bundle(builder.write()))

    assert report.trace.status == "broken"
    assert report.trace.breaks[0].code == "decision_without_source"


def test_unknown_source_reference_broken(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    builder.trace_decision("ep-tech-1", "dec-1", ("src-missing",))
    builder.trace_item("ep-tech-1", "item-1", "dec-1")
    report = derive_report(load_bundle(builder.write()))

    assert report.trace.breaks[0].code == "unknown_source_span"


def test_decision_without_build_item_broken(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    builder.trace_source("ep-tech-1", "src-1")
    builder.trace_decision("ep-tech-1", "dec-1", ("src-1",))
    report = derive_report(load_bundle(builder.write()))

    assert report.trace.breaks[0].code == "decision_without_build_item"


def test_build_item_unknown_decision_broken(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    builder.trace_source("ep-tech-1", "src-1")
    builder.trace_decision("ep-tech-1", "dec-1", ("src-1",))
    builder.trace_item("ep-tech-1", "item-1", "dec-missing")
    report = derive_report(load_bundle(builder.write()))

    assert report.trace.breaks[0].code == "build_item_without_decision"


def test_review_unknown_decision_broken(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    _complete_trace(builder)
    builder.review("ep-tech-1", decision_id="dec-missing", base="v1", result="v2")
    report = derive_report(load_bundle(builder.write()))

    assert report.trace.breaks[0].code == "review_decision_unknown"


def test_cross_episode_reference_broken(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    builder.episode("ep-tech-2", kind="technical")
    builder.trace_source("ep-tech-1", "src-1")
    builder.trace_decision("ep-tech-1", "dec-1", ("src-1",))
    builder.trace_item("ep-tech-2", "item-1", "dec-1")
    report = derive_report(load_bundle(builder.write()))

    assert report.trace.breaks[0].code == "decision_episode_mismatch"


def test_no_trace_data_not_evaluated(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    report = derive_report(load_bundle(builder.write()))

    assert report.trace.status == "not_evaluated"
    assert report.trace.breaks == ()


def test_broken_trace_fails_validation_typed(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    builder.trace_decision("ep-tech-1", "dec-1", ())
    builder.trace_item("ep-tech-1", "item-1", "dec-1")
    bundle = load_bundle(builder.write())
    report = derive_report(bundle)

    failures = validate_report(report, bundle)
    assert [failure.code for failure in failures].count("broken_trace") == 1
