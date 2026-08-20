from __future__ import annotations

from pathlib import Path

from services.metrics.bundle import load_bundle
from services.metrics.derive import derive_report
from tests.metrics.support import BundleBuilder


def test_build_counts_and_stage_coverage_from_stage_events(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    builder.stage("ep-tech-1", "build", "attempt-failed", attempt=1)
    builder.stage("ep-tech-1", "build", "attempt-succeeded", attempt=2)
    builder.stage("ep-tech-1", "render", "attempt-succeeded", attempt=1)
    builder.stage("ep-tech-1", "ingest", "run-blocked")
    builder.stage("ep-tech-1", "analyze", "run-reused")
    builder.stage("ep-tech-1", "compile", "run-recovered")
    report = derive_report(load_bundle(builder.write()))

    assert report.build.stages_observed == ("analyze", "build", "compile", "ingest", "render")
    assert report.build.attempt_failed == 1
    assert report.build.attempt_succeeded == 2
    assert report.build.run_blocked == 1
    assert report.build.run_reused == 1
    assert report.build.run_recovered == 1


def test_qc_aggregation(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    builder.qc_outcome("ep-tech-1", verdict="passed")
    builder.qc_outcome("ep-tech-1", verdict="blocked", blockers=2, major=1)
    builder.qc_outcome("ep-tech-1", verdict="blocked", blockers=1, minor=3)
    report = derive_report(load_bundle(builder.write()))

    assert report.qc.reports == 3
    assert report.qc.passed == 1
    assert report.qc.blocked == 2
    assert report.qc.blocker_issues == 3
    assert report.qc.major_issues == 1
    assert report.qc.minor_issues == 3


def test_storage_by_retention_class(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    builder.artifact("ep-tech-1", "art-1", "authoritative", 1000)
    builder.artifact("ep-tech-1", "art-2", "authoritative", 250)
    builder.artifact("ep-tech-1", "art-3", "rebuildable", 4000)
    builder.artifact("ep-tech-1", "art-4", "runtime_cache", 10)
    builder.artifact("ep-tech-1", "art-5", "manual_finalization", 77)
    report = derive_report(load_bundle(builder.write()))

    assert report.storage.authoritative_bytes == 1250
    assert report.storage.rebuildable_bytes == 4000
    assert report.storage.runtime_cache_bytes == 10
    assert report.storage.manual_finalization_bytes == 77
    assert report.storage.total_bytes == 5337
