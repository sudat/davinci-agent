from __future__ import annotations

from pathlib import Path

from services.metrics.bundle import load_bundle
from services.metrics.derive import derive_report
from services.metrics.report_models import DistributionMetric, TimeSample
from services.metrics.validate import validate_report
from tests.metrics.support import BundleBuilder, full_phase_timing, real_approved_episode

AHT = "active_human_time"


def _aht(report):
    return next(item for item in report.time_metrics if item.metric == AHT)


def test_technical_sample_in_kpi_is_unsupported(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    full_phase_timing(builder, "ep-tech-1")
    bundle = load_bundle(builder.write())
    report = derive_report(bundle)

    tampered_aht = DistributionMetric(
        metric=AHT,
        median_ms=1,
        p90_ms=1,
        sample_count=1,
        samples=(TimeSample(episode_id="ep-tech-1", value_ms=1),),
        not_evaluated=(),
        small_sample=True,
        methodology=_aht(report).methodology,
    )
    tampered = report.model_copy(
        update={
            "time_metrics": (
                tampered_aht,
                *(m for m in report.time_metrics if m.metric != AHT),
            )
        }
    )
    codes = [failure.code for failure in validate_report(tampered, bundle)]
    assert "kpi_claim_unsupported" in codes
    assert "report_drift" in codes


def test_unsupported_median_claim_from_technical_fixtures_typed(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-tech-1", kind="technical")
    full_phase_timing(builder, "ep-tech-1")
    builder.claim("claim-30min", "active_human_time_median_ms", 1_800_000)
    bundle = load_bundle(builder.write())
    report = derive_report(bundle)

    assert report.claims_review[0].supported is False
    codes = [failure.code for failure in validate_report(report, bundle)]
    assert codes.count("kpi_claim_unsupported") == 1


def test_supported_median_claim_passes(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    full_phase_timing(builder, "ep-real-1")
    builder.claim("claim-median", "active_human_time_median_ms", 270_000)
    bundle = load_bundle(builder.write())
    report = derive_report(bundle)

    assert report.claims_review[0].supported is True
    assert validate_report(report, bundle) == ()


def test_wrong_median_claim_value_unsupported(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    full_phase_timing(builder, "ep-real-1")
    builder.claim("claim-median", "active_human_time_median_ms", 120_000)
    bundle = load_bundle(builder.write())
    report = derive_report(bundle)

    assert report.claims_review[0].supported is False
    codes = [failure.code for failure in validate_report(report, bundle)]
    assert "kpi_claim_unsupported" in codes


def test_real_timing_without_genuine_approval_fails(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-real-1")
    full_phase_timing(builder, "ep-real-1")
    bundle = load_bundle(builder.write())
    report = derive_report(bundle)

    codes = [failure.code for failure in validate_report(report, bundle)]
    assert codes.count("false_real_approval") == 1


def test_fixture_approval_record_fails(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    record_id = builder.approval(runner_class="automation", fixture_only=True, uid=None, tty=None)
    builder.episode("ep-real-1", approval_record_id=record_id)
    full_phase_timing(builder, "ep-real-1")
    bundle = load_bundle(builder.write())
    report = derive_report(bundle)

    codes = [failure.code for failure in validate_report(report, bundle)]
    assert "false_real_approval" in codes


def test_hidden_undeclared_episode_fails_validation(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    builder.episode("ep-real-1")
    builder.timing("ep-ghost", "review", "start", 1700000000)
    bundle = load_bundle(builder.write())
    report = derive_report(bundle)

    codes = [failure.code for failure in validate_report(report, bundle)]
    assert codes.count("hidden_out_of_contract") == 1


def test_tampered_report_number_drift(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    full_phase_timing(builder, "ep-real-1")
    bundle = load_bundle(builder.write())
    report = derive_report(bundle)

    eligibility = report.eligibility.model_copy(update={"total_episodes": 99})
    tampered = report.model_copy(update={"eligibility": eligibility})
    codes = [failure.code for failure in validate_report(tampered, bundle)]
    assert "report_drift" in codes


def test_stale_binding_after_bundle_change(tmp_path: Path) -> None:
    builder = BundleBuilder(tmp_path / "bundle")
    real_approved_episode(builder, "ep-real-1")
    full_phase_timing(builder, "ep-real-1")
    root = builder.write()
    bundle = load_bundle(root)
    report = derive_report(bundle)
    assert validate_report(report, bundle) == ()

    with (root / "timing-events.jsonl").open("a") as stream:
        stream.write('{"episode_id":"ep-real-1","phase":"final","marker":"end","timestamp_unix":1700000001,"source":"recorded"}\n')
    codes = [failure.code for failure in validate_report(report, load_bundle(root))]
    assert "stale_binding" in codes
    assert "report_drift" in codes
