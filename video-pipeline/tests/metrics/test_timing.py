from __future__ import annotations

from pathlib import Path

from services.metrics.bundle import load_bundle
from services.metrics.derive import derive_report
from services.metrics.validate import validate_report
from tests.metrics.support import BundleBuilder, full_phase_timing, real_approved_episode

AHT = "active_human_time"


def _metric(report, name: str):
    return next(item for item in report.time_metrics if item.metric == name)


def test_complete_phases_produce_aht_sample_sum(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    full_phase_timing(builder, "ep-real-1")
    report = derive_report(load_bundle(builder.write()))

    aht = _metric(report, AHT)
    assert aht.sample_count == 1
    assert aht.samples[0].episode_id == "ep-real-1"
    assert aht.samples[0].value_ms == (60 + 120 + 30 + 45 + 15) * 1000
    assert aht.median_ms == aht.samples[0].value_ms
    assert aht.p90_ms == aht.samples[0].value_ms
    assert aht.small_sample is True
    for phase in ("session", "review", "correction", "qc", "final"):
        assert _metric(report, phase).sample_count == 1


def test_missing_end_marker_not_evaluated_with_log_gap(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    full_phase_timing(builder, "ep-real-1", review_s=0)
    builder.timing("ep-real-1", "review", "start", 1700000100)
    report = derive_report(load_bundle(builder.write()))

    review = _metric(report, "review")
    assert review.sample_count == 0
    assert review.median_ms is None
    assert review.not_evaluated[0].reason == "missing end marker"
    assert _metric(report, AHT).not_evaluated[0].reason.startswith("incomplete phases")
    flags = [(flag.episode_id, flag.flag) for flag in report.data_quality]
    assert ("ep-real-1", "log_gap") in flags


def test_missing_timestamps_not_evaluated_never_zero(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    builder.phase_timing("ep-real-1", "session", 1700000000, 1700000060)
    builder.phase_timing("ep-real-1", "review", 1700000060, 1700000180)
    builder.phase_timing("ep-real-1", "correction", 1700000180, 1700000210)
    builder.timing("ep-real-1", "qc", "start", 1700000210)
    builder.timing("ep-real-1", "qc", "end", None)
    builder.phase_timing("ep-real-1", "final", 1700000210, 1700000225)
    builder.claim("claim-median", "active_human_time_median_ms", 600_000)
    report = derive_report(load_bundle(builder.write()))

    qc = _metric(report, "qc")
    assert qc.sample_count == 0
    assert qc.median_ms is None
    assert qc.p90_ms is None
    assert qc.not_evaluated[0].reason == "missing timestamp"
    flags = [(flag.episode_id, flag.flag) for flag in report.data_quality]
    assert ("ep-real-1", "missing_timestamp") in flags
    assert all(sample.value_ms > 0 for metric in report.time_metrics for sample in metric.samples)
    assert report.claims_review[0].supported is False

    bundle = load_bundle(builder.root)
    codes = [failure.code for failure in validate_report(report, bundle)]
    assert "kpi_claim_unsupported" in codes


def test_inverted_interval_not_evaluated_with_log_gap(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    full_phase_timing(builder, "ep-real-1")
    builder.timing("ep-real-1", "review", "start", 1700002000)
    builder.timing("ep-real-1", "review", "end", 1700001000)
    report = derive_report(load_bundle(builder.write()))

    review = _metric(report, "review")
    assert review.not_evaluated[0].reason == "inverted interval"
    flags = [(flag.episode_id, flag.flag) for flag in report.data_quality]
    assert ("ep-real-1", "log_gap") in flags


def test_idle_gap_flagged_not_smoothed(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    builder.phase_timing("ep-real-1", "session", 1700000000, 1700000060)
    builder.phase_timing("ep-real-1", "review", 1700007260, 1700007380)
    builder.phase_timing("ep-real-1", "correction", 1700007380, 1700007410)
    builder.phase_timing("ep-real-1", "qc", 1700007410, 1700007455)
    builder.phase_timing("ep-real-1", "final", 1700007455, 1700007470)
    report = derive_report(load_bundle(builder.write()))

    idle = [flag for flag in report.data_quality if flag.flag == "idle_gap"]
    assert idle
    assert idle[0].episode_id == "ep-real-1"
    assert "7200" in idle[0].detail
    assert _metric(report, "session").samples[0].value_ms == 60_000
    assert _metric(report, AHT).samples[0].value_ms == (60 + 120 + 30 + 45 + 15) * 1000


def test_self_report_interval_flagged_but_counted(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    builder.phase_timing("ep-real-1", "session", 1700000000, 1700000060)
    builder.phase_timing("ep-real-1", "review", 1700000060, 1700000180)
    builder.phase_timing("ep-real-1", "correction", 1700000180, 1700000210)
    builder.timing("ep-real-1", "qc", "start", 1700000210)
    builder.timing("ep-real-1", "qc", "end", 1700000255, source="self_report")
    builder.phase_timing("ep-real-1", "final", 1700000255, 1700000270)
    report = derive_report(load_bundle(builder.write()))

    flags = [(flag.episode_id, flag.flag) for flag in report.data_quality]
    assert ("ep-real-1", "self_report_gap") in flags
    assert _metric(report, "qc").sample_count == 1


def test_technical_episodes_never_enter_kpi_samples(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    full_phase_timing(builder, "ep-tech-1")
    report = derive_report(load_bundle(builder.write()))

    assert _metric(report, AHT).sample_count == 0
    assert _metric(report, AHT).median_ms is None
    for phase in ("session", "review", "correction", "qc", "final"):
        assert _metric(report, phase).samples == ()


def test_unapproved_real_episode_not_evaluated(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-real-1")
    full_phase_timing(builder, "ep-real-1")
    report = derive_report(load_bundle(builder.write()))

    review = _metric(report, "review")
    assert review.not_evaluated[0].reason == "no genuine operator approval record"
    assert _metric(report, AHT).sample_count == 0
