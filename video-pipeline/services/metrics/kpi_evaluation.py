"""Phase-9 KPI evaluation harness (task 58 / implementation plan §13).

Aggregates per-episode run reports into one ``kpi-report-v1`` covering the
impl-plan §13 metric list: median/P90 Active Human Time, manual-Resolve and
first-preview trends, TTFRP P50/P90 by source-duration bucket, deep-review
ratio + analysis cost, blocking sessions, Cockpit completion (CLI/JSON/
direct-Resolve-free), seven-domain fallback/block rates, publishability
trend, reference domain-attribution error + cross-domain rates, recipe
override rate, interruptions by class, manual fallback rate, coverage by
footage type. Report generation only — judgement stays with the human.

Honesty contract (PRD 3.1): target statuses are derived ONLY from measured
values. A target is ``achieved``/``not_achieved`` exclusively when at least
``MIN_EPISODES_FOR_TARGET`` measured samples exist and the statistic is
computed; anything less carries ``insufficient_data`` — never an asserted
claim. Null fields contribute no samples; every section exposes its own
``sample_count`` so a partial report is visible as partial.

Percentile method: pure order statistics on float minutes, no interpolation
— lower median (rank ``(n+1)//2``; even n keeps the lower middle) and
nearest-rank P90 (rank ``ceil(0.9*n)``), the float-minutes equivalent of
:mod:`services.metrics.stats`. Trend method: chronological halves (first
``n//2`` vs last ``n//2`` samples by ``sequence_index``; odd n drops the
middle sample); direction only when both halves are non-empty.

Input adapter seam (T18/T23 documented-adapter precedent, see
``services/creative_plan/quality_domains.py``): task 52's
``episode-run-report-v1`` is not committed yet, so the input is typed
LOCALLY as :class:`EpisodeRunReportInputV1` — a minimal mirror carrying
exactly the fields this aggregation needs. When task 52 lands, its report
maps 1:1 onto this mirror (null → unknown); this module's shape does not
change.
"""

# allow: SIZE_OK — task 58 pins the deliverable to this single module and its
# commit path list (input mirror + report models + evaluate_kpis +
# render_kpi_summary in kpi_evaluation.py); T40 single-module commit-scope
# precedent. 625 pure LOC, of which ~260 are StrictModel field tables —
# irreducible enumeration of the impl-plan §13 metric list (19 sections) —
# plus the adapter-seam input mirror; the remainder is deterministic
# aggregation and the Japanese renderer.

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field

from services.contracts.primitives import Identifier, StrictModel
from services.metrics.models import (  # noqa: TC001 (pydantic resolves it at runtime)
    Seq,
)

MIN_EPISODES_FOR_TARGET: Final[int] = 10
SMALL_N_THRESHOLD: Final[int] = 5

KPI_METHODOLOGY: Final[str] = (
    "order statistics on float minutes, no interpolation: lower median "
    "(rank (n+1)//2, even n keeps the lower middle) and nearest-rank P90 "
    "(rank ceil(0.9*n)); trends compare chronological halves (first n//2 vs "
    "last n//2 by sequence_index, odd-n middle sample excluded); target "
    f"statuses require sample_count >= {MIN_EPISODES_FOR_TARGET} measured "
    "samples and are derived only from measured values, never asserted; "
    "null fields contribute no samples (per-section sample_count is the "
    "honest denominator); input is the local EpisodeRunReportInputV1 mirror "
    "of task 52's episode-run-report-v1 (adapter seam)"
)

#: Local mirror of PRD 14.3's seven quality domains (quality_domains.py).
KpiQualityDomain = Literal[
    "editorial_construction",
    "subtitle",
    "audio_finishing",
    "color_finishing",
    "framing_motion",
    "graphics_presentation",
    "delivery_qc",
]

KPI_QUALITY_DOMAINS: Final[tuple[KpiQualityDomain, ...]] = (
    "editorial_construction",
    "subtitle",
    "audio_finishing",
    "color_finishing",
    "framing_motion",
    "graphics_presentation",
    "delivery_qc",
)

DomainStatus = Literal[
    "applied", "intentionally_not_needed", "manual_fallback_required", "blocked"
]

TtfrpBucketId = Literal["lte_90", "gt_90_lte_180", "gt_180"]

TargetId = Literal[
    "aht_median_le_30",
    "aht_p90_le_60",
    "ttfrp_p50_le_30_lte_90",
    "ttfrp_p50_le_60_gt_90_lte_180",
]

_BUCKET_BOUNDS: Final[tuple[tuple[TtfrpBucketId, float, float], ...]] = (
    ("lte_90", 0.0, 90.0),
    ("gt_90_lte_180", 90.0, 180.0),
    ("gt_180", 180.0, float("inf")),
)


class KpiEvaluationError(Exception):
    """Typed failure: unreadable path, invalid payload, duplicate episode."""


def _float_coerce(value: object) -> object:
    """Strict models reject int-for-float; JSON ints are honest minutes too."""

    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


Minutes = Annotated[float, BeforeValidator(_float_coerce), Field(ge=0, strict=True)]
Ratio = Annotated[float, BeforeValidator(_float_coerce), Field(ge=0.0, le=1.0)]


class DomainStatusEntry(StrictModel):
    domain: KpiQualityDomain
    status: DomainStatus


class InterruptionCountsV1(StrictModel):
    safe_auto_resolve: int = Field(default=0, ge=0, strict=True)
    degraded_but_recoverable: int = Field(default=0, ge=0, strict=True)
    human_decision_required: int = Field(default=0, ge=0, strict=True)


class ReferenceReviewStatsV1(StrictModel):
    comments_total: int = Field(ge=0, strict=True)
    domain_attribution_errors: int = Field(default=0, ge=0, strict=True)
    cross_domain_inferences: int = Field(default=0, ge=0, strict=True)


class RecipeUsageV1(StrictModel):
    recipes_used: int = Field(ge=0, strict=True)
    recipes_overridden: int = Field(default=0, ge=0, strict=True)


class EpisodeRunReportInputV1(StrictModel):
    """Minimal local mirror of task 52's episode-run-report-v1 (docstring)."""

    episode_id: Identifier
    sequence_index: int = Field(ge=0, strict=True)
    active_human_time_minutes: Minutes | None = None
    manual_resolve_edit_minutes: Minutes | None = None
    first_preview_accepted: bool | None = None
    ttfrp_minutes: Minutes | None = None
    source_duration_minutes: Minutes | None = None
    deep_review_ratio: Ratio | None = None
    analysis_cost_per_source_minute: Minutes | None = None
    blocking_sessions: int | None = Field(default=None, ge=0, strict=True)
    cockpit_completed_free: bool | None = None
    domain_statuses: Seq[DomainStatusEntry] = ()
    publishable_after_first_review: bool | None = None
    reference_review: ReferenceReviewStatsV1 | None = None
    recipe_usage: RecipeUsageV1 | None = None
    interruptions: InterruptionCountsV1 | None = None
    manual_fallback_used: bool | None = None
    footage_types: Seq[str] = ()


class TrendSectionV1(StrictModel):
    direction: Literal["improving", "degrading", "flat", "insufficient_data"]
    polarity: Literal["lower_is_better", "higher_is_better"]
    first_half_mean: float | None
    second_half_mean: float | None
    first_half_n: int = Field(ge=0, strict=True)
    second_half_n: int = Field(ge=0, strict=True)


class DistributionSectionV1(StrictModel):
    median_minutes: float | None
    p90_minutes: float | None
    sample_count: int = Field(ge=0, strict=True)
    not_evaluated_count: int = Field(ge=0, strict=True)
    small_sample: bool
    trend: TrendSectionV1


class RateSectionV1(StrictModel):
    rate: float | None = Field(default=None, ge=0.0, le=1.0)
    numerator: int = Field(ge=0, strict=True)
    sample_count: int = Field(ge=0, strict=True)
    trend: TrendSectionV1


class TtfrpBucketV1(StrictModel):
    bucket: TtfrpBucketId
    p50_minutes: float | None
    p90_minutes: float | None
    sample_count: int = Field(ge=0, strict=True)


class MedianSectionV1(StrictModel):
    median: float | None
    sample_count: int = Field(ge=0, strict=True)


class TotalMedianSectionV1(StrictModel):
    total: int = Field(ge=0, strict=True)
    median_per_episode: float | None
    sample_count: int = Field(ge=0, strict=True)


class DomainRateRowV1(StrictModel):
    domain: KpiQualityDomain
    sample_count: int = Field(ge=0, strict=True)
    manual_fallback_count: int = Field(ge=0, strict=True)
    blocked_count: int = Field(ge=0, strict=True)
    manual_fallback_rate: Ratio | None = None
    blocked_rate: Ratio | None = None


class ReferenceLearningSectionV1(StrictModel):
    comments_total: int = Field(ge=0, strict=True)
    domain_attribution_errors_total: int = Field(ge=0, strict=True)
    cross_domain_inferences_total: int = Field(ge=0, strict=True)
    domain_attribution_error_rate: Ratio | None = None
    cross_domain_inference_rate: Ratio | None = None
    episodes_reporting: int = Field(ge=0, strict=True)


class RecipeOverrideSectionV1(StrictModel):
    used_total: int = Field(ge=0, strict=True)
    overridden_total: int = Field(ge=0, strict=True)
    rate: Ratio | None = None
    episodes_reporting: int = Field(ge=0, strict=True)


class SimpleRateSectionV1(StrictModel):
    rate: Ratio | None = None
    used_count: int = Field(ge=0, strict=True)
    sample_count: int = Field(ge=0, strict=True)


class CoverageRowV1(StrictModel):
    footage_type: str = Field(min_length=1, strict=True)
    episode_count: int = Field(ge=0, strict=True)
    rate: Ratio


class TargetAssessmentV1(StrictModel):
    target_id: TargetId
    threshold_minutes: float
    status: Literal["achieved", "not_achieved", "insufficient_data"]
    measured_minutes: float | None
    sample_count: int = Field(ge=0, strict=True)
    detail: str = Field(min_length=1, strict=True)


class KpiReportV1(StrictModel):
    schema_version: Literal["kpi-report-v1"] = "kpi-report-v1"
    episode_ids: Seq[Identifier]
    episode_count: int = Field(ge=0, strict=True)
    insufficient_data: bool
    active_human_time: DistributionSectionV1
    manual_resolve_trend: TrendSectionV1
    first_preview: RateSectionV1
    ttfrp_buckets: Seq[TtfrpBucketV1]
    deep_review: MedianSectionV1
    analysis_cost: MedianSectionV1
    blocking_sessions: TotalMedianSectionV1
    cockpit_completion: RateSectionV1
    domain_rates: Seq[DomainRateRowV1]
    publishability: RateSectionV1
    reference_learning: ReferenceLearningSectionV1
    recipe_override: RecipeOverrideSectionV1
    interruptions: InterruptionCountsV1
    manual_fallback: SimpleRateSectionV1
    coverage: Seq[CoverageRowV1]
    targets: Seq[TargetAssessmentV1]
    methodology: str = KPI_METHODOLOGY


def _lower_median(values: Sequence[float]) -> float:
    return sorted(values)[(len(values) + 1) // 2 - 1]


def _nearest_rank_p90(values: Sequence[float]) -> float:
    return sorted(values)[-(-9 * len(values) // 10) - 1]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _trend(
    values: Sequence[float], polarity: Literal["lower_is_better", "higher_is_better"]
) -> TrendSectionV1:
    n = len(values)
    first, second = values[: n // 2], values[n - n // 2 :]
    if not first or not second:
        return TrendSectionV1(
            direction="insufficient_data",
            polarity=polarity,
            first_half_mean=None,
            second_half_mean=None,
            first_half_n=len(first),
            second_half_n=len(second),
        )
    first_mean, second_mean = _mean(first), _mean(second)
    direction: Literal["improving", "degrading", "flat"]
    if second_mean == first_mean:
        direction = "flat"
    elif (second_mean < first_mean) == (polarity == "lower_is_better"):
        direction = "improving"
    else:
        direction = "degrading"
    return TrendSectionV1(
        direction=direction,
        polarity=polarity,
        first_half_mean=first_mean,
        second_half_mean=second_mean,
        first_half_n=len(first),
        second_half_n=len(second),
    )


def _distribution(values: Sequence[float], total: int) -> DistributionSectionV1:
    return DistributionSectionV1(
        median_minutes=_lower_median(values) if values else None,
        p90_minutes=_nearest_rank_p90(values) if values else None,
        sample_count=len(values),
        not_evaluated_count=total - len(values),
        small_sample=len(values) < SMALL_N_THRESHOLD,
        trend=_trend(values, "lower_is_better"),
    )


def _rate(
    flags: Sequence[bool], polarity: Literal["lower_is_better", "higher_is_better"]
) -> RateSectionV1:
    ones = [1.0 if flag else 0.0 for flag in flags]
    return RateSectionV1(
        rate=(sum(ones) / len(ones)) if ones else None,
        numerator=int(sum(ones)),
        sample_count=len(ones),
        trend=_trend(ones, polarity),
    )


def _ttfrp_buckets(
    reports: Sequence[EpisodeRunReportInputV1],
) -> tuple[TtfrpBucketV1, ...]:
    rows = []
    for bucket, lo, hi in _BUCKET_BOUNDS:
        vals = [
            r.ttfrp_minutes
            for r in reports
            if r.ttfrp_minutes is not None
            and r.source_duration_minutes is not None
            and lo < r.source_duration_minutes <= hi
        ]
        rows.append(
            TtfrpBucketV1(
                bucket=bucket,
                p50_minutes=_lower_median(vals) if vals else None,
                p90_minutes=_nearest_rank_p90(vals) if vals else None,
                sample_count=len(vals),
            )
        )
    return tuple(rows)


def _domain_rates(
    reports: Sequence[EpisodeRunReportInputV1],
) -> tuple[DomainRateRowV1, ...]:
    rows = []
    for domain in KPI_QUALITY_DOMAINS:
        statuses = [
            entry.status
            for r in reports
            for entry in r.domain_statuses
            if entry.domain == domain
        ]
        fallback = statuses.count("manual_fallback_required")
        blocked = statuses.count("blocked")
        rows.append(
            DomainRateRowV1(
                domain=domain,
                sample_count=len(statuses),
                manual_fallback_count=fallback,
                blocked_count=blocked,
                manual_fallback_rate=(fallback / len(statuses)) if statuses else None,
                blocked_rate=(blocked / len(statuses)) if statuses else None,
            )
        )
    return tuple(rows)


def _target(
    target_id: TargetId, threshold: float, measured: float | None, n: int
) -> TargetAssessmentV1:
    status: Literal["achieved", "not_achieved", "insufficient_data"]
    if measured is None:
        status = "insufficient_data"
        detail = f"no measured samples (n={n})"
    elif n < MIN_EPISODES_FOR_TARGET:
        status = "insufficient_data"
        detail = f"sample_count {n} < {MIN_EPISODES_FOR_TARGET}"
    elif measured <= threshold:
        status = "achieved"
        detail = f"measured {measured} <= {threshold} (n={n})"
    else:
        status = "not_achieved"
        detail = f"measured {measured} > {threshold} (n={n})"
    return TargetAssessmentV1(
        target_id=target_id,
        threshold_minutes=threshold,
        status=status,
        measured_minutes=measured,
        sample_count=n,
        detail=detail,
    )


def _load(source: EpisodeRunReportInputV1 | Path | str) -> EpisodeRunReportInputV1:
    if isinstance(source, EpisodeRunReportInputV1):
        return source
    path = Path(source)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return EpisodeRunReportInputV1.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise KpiEvaluationError(f"{path}: {exc}") from exc


def _values(reports: Sequence[EpisodeRunReportInputV1], field: str) -> list[float]:
    return [v for r in reports if (v := getattr(r, field)) is not None]


def _evaluate_interruptions(
    reports: Sequence[EpisodeRunReportInputV1],
) -> InterruptionCountsV1:
    return InterruptionCountsV1(
        safe_auto_resolve=sum(
            r.interruptions.safe_auto_resolve
            for r in reports
            if r.interruptions is not None
        ),
        degraded_but_recoverable=sum(
            r.interruptions.degraded_but_recoverable
            for r in reports
            if r.interruptions is not None
        ),
        human_decision_required=sum(
            r.interruptions.human_decision_required
            for r in reports
            if r.interruptions is not None
        ),
    )


def evaluate_kpis(
    run_reports: Sequence[EpisodeRunReportInputV1 | Path | str],
) -> KpiReportV1:
    """Aggregate episode run reports into one ``kpi-report-v1``.

    Reports are ordered by ``(sequence_index, episode_id)`` for trends and
    output; generation always succeeds (N<10 → ``insufficient_data``).
    """

    reports = sorted(
        (_load(source) for source in run_reports),
        key=lambda r: (r.sequence_index, r.episode_id),
    )
    ids = [r.episode_id for r in reports]
    if len(set(ids)) != len(ids):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        raise KpiEvaluationError(f"duplicate episode_id: {', '.join(duplicates)}")

    n_total = len(reports)
    aht = _values(reports, "active_human_time_minutes")
    manual = _values(reports, "manual_resolve_edit_minutes")
    deep = _values(reports, "deep_review_ratio")
    cost = _values(reports, "analysis_cost_per_source_minute")
    blocking = _values(reports, "blocking_sessions")
    first_preview = _rate(
        [v for v in (r.first_preview_accepted for r in reports) if v is not None],
        "higher_is_better",
    )
    publishability = _rate(
        [
            v
            for v in (r.publishable_after_first_review for r in reports)
            if v is not None
        ],
        "higher_is_better",
    )
    cockpit = _rate(
        [v for v in (r.cockpit_completed_free for r in reports) if v is not None],
        "higher_is_better",
    )
    buckets = {b.bucket: b for b in _ttfrp_buckets(reports)}
    fallback_flags = [
        v for v in (r.manual_fallback_used for r in reports) if v is not None
    ]
    comments = sum(
        r.reference_review.comments_total
        for r in reports
        if r.reference_review is not None
    )
    attr_errors = sum(
        r.reference_review.domain_attribution_errors
        for r in reports
        if r.reference_review is not None
    )
    cross = sum(
        r.reference_review.cross_domain_inferences
        for r in reports
        if r.reference_review is not None
    )
    used = sum(
        r.recipe_usage.recipes_used for r in reports if r.recipe_usage is not None
    )
    overridden = sum(
        r.recipe_usage.recipes_overridden
        for r in reports
        if r.recipe_usage is not None
    )
    coverage: dict[str, int] = {}
    for r in reports:
        for footage in set(r.footage_types):
            coverage[footage] = coverage.get(footage, 0) + 1
    return KpiReportV1(
        episode_ids=tuple(ids),
        episode_count=n_total,
        insufficient_data=n_total < MIN_EPISODES_FOR_TARGET,
        active_human_time=_distribution(aht, n_total),
        manual_resolve_trend=_trend(manual, "lower_is_better"),
        first_preview=first_preview,
        ttfrp_buckets=_ttfrp_buckets(reports),
        deep_review=MedianSectionV1(
            median=_lower_median(deep) if deep else None, sample_count=len(deep)
        ),
        analysis_cost=MedianSectionV1(
            median=_lower_median(cost) if cost else None, sample_count=len(cost)
        ),
        blocking_sessions=TotalMedianSectionV1(
            total=int(sum(blocking)),
            median_per_episode=float(_lower_median(blocking)) if blocking else None,
            sample_count=len(blocking),
        ),
        cockpit_completion=cockpit,
        domain_rates=_domain_rates(reports),
        publishability=publishability,
        reference_learning=ReferenceLearningSectionV1(
            comments_total=comments,
            domain_attribution_errors_total=attr_errors,
            cross_domain_inferences_total=cross,
            domain_attribution_error_rate=(attr_errors / comments) if comments else None,
            cross_domain_inference_rate=(cross / comments) if comments else None,
            episodes_reporting=sum(r.reference_review is not None for r in reports),
        ),
        recipe_override=RecipeOverrideSectionV1(
            used_total=used,
            overridden_total=overridden,
            rate=(overridden / used) if used else None,
            episodes_reporting=sum(r.recipe_usage is not None for r in reports),
        ),
        interruptions=_evaluate_interruptions(reports),
        manual_fallback=SimpleRateSectionV1(
            rate=(sum(fallback_flags) / len(fallback_flags)) if fallback_flags else None,
            used_count=int(sum(fallback_flags)),
            sample_count=len(fallback_flags),
        ),
        coverage=tuple(
            CoverageRowV1(footage_type=f, episode_count=c, rate=c / n_total)
            for f, c in sorted(coverage.items())
        ),
        targets=(
            _target(
                "aht_median_le_30",
                30.0,
                _lower_median(aht) if aht else None,
                len(aht),
            ),
            _target(
                "aht_p90_le_60",
                60.0,
                _nearest_rank_p90(aht) if aht else None,
                len(aht),
            ),
            _target(
                "ttfrp_p50_le_30_lte_90",
                30.0,
                buckets["lte_90"].p50_minutes,
                buckets["lte_90"].sample_count,
            ),
            _target(
                "ttfrp_p50_le_60_gt_90_lte_180",
                60.0,
                buckets["gt_90_lte_180"].p50_minutes,
                buckets["gt_90_lte_180"].sample_count,
            ),
        ),
    )


_TARGET_LABELS: Final[dict[str, str]] = {
    "aht_median_le_30": "AHT中央値 ≤30分",
    "aht_p90_le_60": "AHT P90 ≤60分",
    "ttfrp_p50_le_30_lte_90": "TTFRP P50 ≤30分 (≤90分素材)",
    "ttfrp_p50_le_60_gt_90_lte_180": "TTFRP P50 ≤60分 (90-180分素材)",
}
_TARGET_STATUS_JA: Final[dict[str, str]] = {
    "achieved": "達成",
    "not_achieved": "未達成",
    "insufficient_data": "データ不足",
}
_DIRECTION_JA: Final[dict[str, str]] = {
    "improving": "改善",
    "degrading": "悪化",
    "flat": "横ばい",
    "insufficient_data": "データ不足",
}


def _trend_line(label: str, trend: TrendSectionV1) -> str:
    if trend.direction == "insufficient_data":
        return f"{label}: 傾向 データ不足 (n={trend.first_half_n}/{trend.second_half_n})"
    return (
        f"{label}: 傾向 {_DIRECTION_JA[trend.direction]} "
        f"(前半平均 {trend.first_half_mean} → 後半平均 {trend.second_half_mean}, "
        f"n={trend.first_half_n}/{trend.second_half_n})"
    )


def render_kpi_summary(report: KpiReportV1) -> str:
    """Human-readable Japanese summary — numbers only from measured data."""

    flag = "はい (10話未満)" if report.insufficient_data else "いいえ"
    lines = [
        f"KPIサマリー (kpi-report-v1, 対象 {report.episode_count}話)",
        f"データ不足: {flag}",
    ]
    aht = report.active_human_time
    lines.append(
        f"Active Human Time: 中央値 {aht.median_minutes}分 / P90 {aht.p90_minutes}分 "
        f"(n={aht.sample_count}, 未計測 {aht.not_evaluated_count}話)"
    )
    lines.append(_trend_line("  AHT傾向", aht.trend))
    lines.append("目標判定 (実測のみから導出):")
    for target in report.targets:
        measured = (
            f"実測 {target.measured_minutes}分"
            if target.measured_minutes is not None
            else "実測なし"
        )
        lines.append(
            f"  - {_TARGET_LABELS[target.target_id]}: "
            f"{_TARGET_STATUS_JA[target.status]} ({measured}, n={target.sample_count})"
        )
    lines.append(_trend_line("manual Resolve編集", report.manual_resolve_trend))
    fp = report.first_preview
    lines.append(f"First Preview受入率: {fp.rate} (n={fp.sample_count})")
    lines.append(_trend_line("  受入傾向", fp.trend))
    lines.extend(
        f"TTFRP {bucket.bucket}: P50 {bucket.p50_minutes}分 / "
        f"P90 {bucket.p90_minutes}分 (n={bucket.sample_count})"
        for bucket in report.ttfrp_buckets
    )
    lines.append(
        f"深掘り率中央値: {report.deep_review.median} (n={report.deep_review.sample_count})"
    )
    lines.append(
        f"分析cost/素材分 中央値: {report.analysis_cost.median} "
        f"(n={report.analysis_cost.sample_count})"
    )
    blocking = report.blocking_sessions
    lines.append(
        f"人的blockセッション: 合計 {blocking.total} / "
        f"中央値 {blocking.median_per_episode} (n={blocking.sample_count})"
    )
    cockpit = report.cockpit_completion
    lines.append(
        f"Cockpit完走率 (CLI/JSON/直接Resolveなし): {cockpit.rate} "
        f"(n={cockpit.sample_count})"
    )
    lines.append("7領域 fallback/block:")
    lines.extend(
        f"  - {row.domain}: fallback {row.manual_fallback_count}/{row.sample_count}"
        f" ({row.manual_fallback_rate}) /"
        f" block {row.blocked_count}/{row.sample_count} ({row.blocked_rate})"
        for row in report.domain_rates
    )
    pub = report.publishability
    lines.append(f"初回レビュー後publishable率: {pub.rate} (n={pub.sample_count})")
    lines.append(_trend_line("  publishability傾向", pub.trend))
    ref = report.reference_learning
    lines.append(
        f"参照ドメイン帰属error率: {ref.domain_attribution_error_rate} / "
        f"交差dump率: {ref.cross_domain_inference_rate} "
        f"(コメント {ref.comments_total}, 対象 {ref.episodes_reporting}話)"
    )
    recipe = report.recipe_override
    lines.append(
        f"recipe override率: {recipe.rate} "
        f"({recipe.overridden_total}/{recipe.used_total}, n={recipe.episodes_reporting})"
    )
    ints = report.interruptions
    lines.append(
        f"割込み: SAFE_AUTO_RESOLVE {ints.safe_auto_resolve} / "
        f"DEGRADED_BUT_RECOVERABLE {ints.degraded_but_recoverable} / "
        f"HUMAN_DECISION_REQUIRED {ints.human_decision_required}"
    )
    mf = report.manual_fallback
    lines.append(f"manual fallback率: {mf.rate} ({mf.used_count}/{mf.sample_count})")
    lines.append("footage type別coverage:")
    lines.extend(
        f"  - {row.footage_type}: {row.episode_count}話 ({row.rate})"
        for row in report.coverage
    )
    lines.append(f"手法: {report.methodology}")
    return "\n".join(lines)


__all__ = [
    "MIN_EPISODES_FOR_TARGET",
    "EpisodeRunReportInputV1",
    "KpiEvaluationError",
    "KpiReportV1",
    "evaluate_kpis",
    "render_kpi_summary",
]
