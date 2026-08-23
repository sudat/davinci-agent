"""Task 58: Phase-9 KPI evaluation harness (impl-plan §13).

Synthetic EpisodeRunReportInputV1 fixtures only — no target claim is ever
made from these tests against real episodes (PRD 3.1: synthetic fixtures
never support a target claim; here we only check the harness's honest
reporting, including that it REFUSES claims the data cannot support).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from services.cli.kpi import main as kpi_main
from services.foundation_io import canonical_model_bytes
from services.metrics.kpi_evaluation import EpisodeRunReportInputV1 as Report
from services.metrics.kpi_evaluation import (
    KpiEvaluationError,
    KpiReportV1,
    evaluate_kpis,
    render_kpi_summary,
)

DOMAINS = (
    "editorial_construction",
    "subtitle",
    "audio_finishing",
    "color_finishing",
    "framing_motion",
    "graphics_presentation",
    "delivery_qc",
)


def _full_statuses(spec: dict[str, str]) -> tuple[dict[str, str], ...]:
    base: dict[str, str] = dict.fromkeys(DOMAINS, "applied")
    base.update(spec)
    return tuple({"domain": d, "status": s} for d, s in base.items())


def make_report(i: int, **overrides: object) -> Report:
    payload: dict[str, object] = {
        "episode_id": f"ep-{i:02d}",
        "sequence_index": i,
        "active_human_time_minutes": [25, 40, 22, 35, 18, 32, 20, 28, 24, 26][i % 10],
        "manual_resolve_edit_minutes": 10.0 - i,
        "first_preview_accepted": i >= 3,
        "ttfrp_minutes": [25, 31, 20, 28, 24, 30, 55, 50, 58, 52][i % 10],
        "source_duration_minutes": 60 if i < 6 else 120,
        "deep_review_ratio": [0.2, 0.1, 0.15, 0.25, 0.3, 0.05, 0.18, 0.22, 0.12, 0.08][
            i % 10
        ],
        "analysis_cost_per_source_minute": 0.5 + 0.1 * i,
        "blocking_sessions": [2, 1, 0, 2, 1, 3, 0, 1, 2, 0][i % 10],
        "cockpit_completed_free": i not in (8, 9),
        "domain_statuses": [
            {"domain": d, "status": s}
            for d, s in (
                dict.fromkeys(DOMAINS, "applied")
                | ({"subtitle": "manual_fallback_required"} if i in (1, 4) else {})
                | ({"graphics_presentation": "blocked"} if i == 6 else {})
            ).items()
        ],
        "publishable_after_first_review": i >= 5,
        "reference_review": {
            "comments_total": 10,
            "domain_attribution_errors": 1,
            "cross_domain_inferences": 1 if i == 1 else 0,
        }
        if i < 2
        else None,
        "recipe_usage": {
            "recipes_used": 2,
            "recipes_overridden": 1 if i == 0 else 0,
        },
        "interruptions": {
            "safe_auto_resolve": 2 if i == 0 else 0,
            "degraded_but_recoverable": 1 if i == 0 else 0,
            "human_decision_required": 1 if i == 1 else 0,
        },
        "manual_fallback_used": i in (2, 7),
        "footage_types": ("talking-head", "b-roll") if i < 2 else ("travel-pov",),
    }
    payload.update(overrides)
    return Report.model_validate(payload)


@pytest.fixture
def ten_reports() -> list[Report]:
    return [make_report(i) for i in range(10)]


def test_a_ten_reports_all_metrics_and_targets(ten_reports: list[Report]) -> None:
    report = evaluate_kpis(ten_reports)
    assert report.episode_count == 10
    assert report.insufficient_data is False
    # AHT: sorted 18,20,22,24,25,26,28,32,35,40 → lower median 25, P90 rank 9 → 35.
    assert report.active_human_time.median_minutes == 25.0
    assert report.active_human_time.p90_minutes == 35.0
    assert report.active_human_time.sample_count == 10
    # Targets: AHT claims only from measurement; TTFRP buckets have n=6/4 < 10.
    statuses = {t.target_id: t for t in report.targets}
    assert statuses["aht_median_le_30"].status == "achieved"
    assert statuses["aht_median_le_30"].measured_minutes == 25.0
    assert statuses["aht_p90_le_60"].status == "achieved"
    assert statuses["ttfrp_p50_le_30_lte_90"].status == "insufficient_data"
    assert statuses["ttfrp_p50_le_30_lte_90"].sample_count == 6
    assert statuses["ttfrp_p50_le_60_gt_90_lte_180"].status == "insufficient_data"
    assert statuses["ttfrp_p50_le_60_gt_90_lte_180"].sample_count == 4
    # TTFRP buckets hand-computed.
    buckets = {b.bucket: b for b in report.ttfrp_buckets}
    assert buckets["lte_90"].sample_count == 6
    assert buckets["lte_90"].p50_minutes == 25.0
    assert buckets["lte_90"].p90_minutes == 31.0
    assert buckets["gt_90_lte_180"].sample_count == 4
    assert buckets["gt_90_lte_180"].p50_minutes == 52.0
    assert buckets["gt_90_lte_180"].p90_minutes == 58.0
    assert buckets["gt_180"].sample_count == 0
    assert buckets["gt_180"].p50_minutes is None
    # Rates and counts.
    assert report.first_preview.rate == pytest.approx(0.7)
    assert report.first_preview.sample_count == 10
    assert report.cockpit_completion.rate == pytest.approx(0.8)
    assert report.publishability.rate == pytest.approx(0.5)
    assert report.manual_fallback.rate == pytest.approx(0.2)
    assert report.recipe_override.rate == pytest.approx(1 / 20)
    assert report.recipe_override.overridden_total == 1
    assert report.recipe_override.used_total == 20
    assert report.deep_review.median == 0.15
    assert report.blocking_sessions.total == 12
    assert report.blocking_sessions.median_per_episode == 1
    assert report.interruptions.safe_auto_resolve == 2
    assert report.interruptions.degraded_but_recoverable == 1
    assert report.interruptions.human_decision_required == 1
    assert report.reference_learning.comments_total == 20
    assert report.reference_learning.domain_attribution_error_rate == pytest.approx(0.1)
    assert report.reference_learning.cross_domain_inference_rate == pytest.approx(0.05)


def test_a2_domain_and_coverage_sections(ten_reports: list[Report]) -> None:
    report = evaluate_kpis(ten_reports)
    # Seven-domain rows: always all seven, in canonical order.
    assert tuple(row.domain for row in report.domain_rates) == DOMAINS
    by_domain = {row.domain: row for row in report.domain_rates}
    assert by_domain["subtitle"].manual_fallback_count == 2
    assert by_domain["subtitle"].sample_count == 10
    assert by_domain["graphics_presentation"].blocked_count == 1
    assert by_domain["color_finishing"].blocked_count == 0
    # Coverage: talking-head appears in 2/10, travel-pov in 8/10, sorted rows.
    coverage = {row.footage_type: row for row in report.coverage}
    assert coverage["talking-head"].episode_count == 2
    assert coverage["travel-pov"].episode_count == 8
    assert coverage["travel-pov"].rate == pytest.approx(0.8)
    assert [row.footage_type for row in report.coverage] == sorted(coverage)


def test_b_four_reports_insufficient_data() -> None:
    reports = [
        make_report(
            i,
            active_human_time_minutes=v,
            ttfrp_minutes=v,
            source_duration_minutes=60,
        )
        for i, v in enumerate([1.0, 2.0, 3.0, 4.0])
    ]
    report = evaluate_kpis(reports)
    assert report.episode_count == 4
    assert report.insufficient_data is True
    # Hand-computed small-n order statistics: median rank 2 → 2, P90 rank 4 → 4.
    assert report.active_human_time.median_minutes == 2.0
    assert report.active_human_time.p90_minutes == 4.0
    assert report.active_human_time.sample_count == 4
    # Every target is insufficient_data below the 10-episode floor.
    assert all(t.status == "insufficient_data" for t in report.targets)
    # Per-metric data counts are present even though generation succeeded.
    assert report.active_human_time.sample_count == 4
    assert report.first_preview.sample_count == 4


def test_c_percentile_math_known_set() -> None:
    values = [float(v) for v in range(10, 101, 10)]  # 10..100 step 10
    reports = [
        make_report(i, active_human_time_minutes=v) for i, v in enumerate(values)
    ]
    report = evaluate_kpis(reports)
    assert report.active_human_time.median_minutes == 50.0
    assert report.active_human_time.p90_minutes == 90.0


def test_d_trend_direction_detection() -> None:
    improving = [
        make_report(i, active_human_time_minutes=40.0 - 3.0 * i) for i in range(10)
    ]
    report = evaluate_kpis(improving)
    assert report.active_human_time.trend.direction == "improving"
    assert report.active_human_time.trend.first_half_n == 5
    assert report.active_human_time.trend.second_half_n == 5
    degrading = [
        make_report(i, active_human_time_minutes=10.0 + 3.0 * i) for i in range(10)
    ]
    assert evaluate_kpis(degrading).active_human_time.trend.direction == "degrading"
    flat = [make_report(i, active_human_time_minutes=20.0) for i in range(10)]
    assert evaluate_kpis(flat).active_human_time.trend.direction == "flat"
    # Rate trends improve when acceptance rises in the second half.
    rising = [make_report(i, first_preview_accepted=i >= 5) for i in range(10)]
    rising_report = evaluate_kpis(rising)
    assert rising_report.first_preview.trend.direction == "improving"
    # Fewer than two usable points → insufficient_data, never a guess.
    lonely = [make_report(0, active_human_time_minutes=10.0)]
    assert evaluate_kpis(lonely).active_human_time.trend.direction == "insufficient_data"


def test_e_targets_never_fabricated() -> None:
    nulls = [make_report(i, active_human_time_minutes=None) for i in range(10)]
    report = evaluate_kpis(nulls)
    assert report.active_human_time.sample_count == 0
    assert report.active_human_time.median_minutes is None
    assert report.active_human_time.p90_minutes is None
    statuses = {t.target_id: t for t in report.targets}
    assert statuses["aht_median_le_30"].status == "insufficient_data"
    assert statuses["aht_median_le_30"].measured_minutes is None
    assert statuses["aht_p90_le_60"].status == "insufficient_data"
    # A genuinely missed target stays not_achieved — never spun.
    slow = [make_report(i, active_human_time_minutes=45.0 + i) for i in range(10)]
    slow_statuses = {t.target_id: t for t in evaluate_kpis(slow).targets}
    assert slow_statuses["aht_median_le_30"].status == "not_achieved"


def test_f_round_trip_and_path_input(
    ten_reports: list[Report], tmp_path: Path
) -> None:
    report = evaluate_kpis(ten_reports)
    revived = KpiReportV1.model_validate(json.loads(canonical_model_bytes(report)))
    assert revived == report
    # Deterministic: a second evaluation yields byte-identical canonical JSON.
    again = evaluate_kpis(list(reversed(ten_reports)))
    assert canonical_model_bytes(again) == canonical_model_bytes(report)
    # Paths are accepted alongside models (mixed) and give the same result.
    paths = []
    for i, rep in enumerate(ten_reports[:3]):
        p = tmp_path / f"run-{i}.json"
        p.write_bytes(canonical_model_bytes(rep))
        paths.append(p)
    mixed = evaluate_kpis([*paths, *ten_reports[3:]])
    assert mixed == report


def test_f_malformed_inputs(ten_reports: list[Report], tmp_path: Path) -> None:
    duplicate = [make_report(0), make_report(0)]
    with pytest.raises(KpiEvaluationError):
        evaluate_kpis(duplicate)
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json")
    with pytest.raises(KpiEvaluationError):
        evaluate_kpis([garbage])
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"episode_id": "ep-x"}))
    with pytest.raises(KpiEvaluationError):
        evaluate_kpis([invalid])
    # Null-heavy reports parse; every metric reports zero samples, no crash.
    sparse = Report.model_validate({"episode_id": "ep-null", "sequence_index": 0})
    sparse_report = evaluate_kpis([sparse])
    assert sparse_report.episode_count == 1
    assert sparse_report.insufficient_data is True
    assert sparse_report.active_human_time.sample_count == 0
    assert sparse_report.active_human_time.median_minutes is None
    assert all(row.sample_count == 0 for row in sparse_report.domain_rates)
    assert sparse_report.coverage == ()


def test_g_summary_contains_key_numbers(ten_reports: list[Report]) -> None:
    summary = render_kpi_summary(evaluate_kpis(ten_reports))
    assert "KPI" in summary
    assert "25.0" in summary
    assert "35.0" in summary
    assert "n=10" in summary
    assert "達成" in summary
    assert "改善" in summary  # AHT halves 28.0 → 26.0
    # The insufficient case is named, never silently green.
    sparse_summary = render_kpi_summary(
        evaluate_kpis([make_report(0), make_report(1)])
    )
    assert "データ不足" in sparse_summary


def test_cli_evaluate_and_summary(ten_reports: list[Report], tmp_path: Path) -> None:
    report_path = tmp_path / "kpi.json"
    inputs = []
    for i, rep in enumerate(ten_reports):
        p = tmp_path / f"run-{i}.json"
        p.write_bytes(canonical_model_bytes(rep))
        inputs.append(p)
    argv = ["evaluate", "--report", *[str(p) for p in inputs], "--out", str(report_path)]
    rc = kpi_main(argv)
    assert rc == 0
    payload = json.loads(report_path.read_text())
    assert payload["schema_version"] == "kpi-report-v1"
    assert payload["episode_count"] == 10
    rc = kpi_main(["summary", "--report", str(report_path)])
    assert rc == 0
    rc = kpi_main(["evaluate", "--report", str(tmp_path / "missing.json")])
    assert rc == 1


def test_cli_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "services.cli.kpi", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "evaluate" in result.stdout
    assert "summary" in result.stdout
