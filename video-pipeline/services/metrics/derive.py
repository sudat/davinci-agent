"""Derive the MetricsReport from a hash-bound event bundle.

Every section is a pure function of the bundle bytes; nothing is
guessed, averaged over gaps, or borrowed from wall-clock time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.metrics.aggregates import (
    build_section,
    corrections_section,
    first_pass_section,
    qc_section,
    storage_section,
)
from services.metrics.honesty import evaluate_claim, genuine_operator_approval
from services.metrics.models import (
    ACTIVE_HUMAN_TIME,
    TIMING_PHASES,
)
from services.metrics.report_models import (
    DistributionMetric,
    EligibilitySection,
    ExclusionEntry,
    MetricName,
    MetricsReport,
    NotEvaluated,
    TimeSample,
)
from services.metrics.stats import (
    SMALL_N_THRESHOLD,
    TIME_METHODOLOGY,
    lower_median,
    nearest_rank_p90,
)
from services.metrics.timing import TimingDerivation, derive_timing
from services.metrics.trace import trace_section

if TYPE_CHECKING:
    from services.metrics.bundle import EventBundle


def _event_episode_ids(bundle: EventBundle) -> set[str]:
    ids = {event.episode_id for event in bundle.timing_events}
    ids |= {wrapped.episode_id for wrapped in bundle.review_events}
    ids |= {wrapped.episode_id for wrapped in bundle.stage_events}
    ids |= {outcome.episode_id for outcome in bundle.qc_outcomes}
    ids |= {record.episode_id for record in bundle.inventory}
    ids |= {record.episode_id for record in bundle.trace_sources}
    ids |= {record.episode_id for record in bundle.trace_decisions}
    ids |= {record.episode_id for record in bundle.trace_build_items}
    return ids


def _distribution(
    metric: MetricName,
    samples: list[TimeSample],
    not_evaluated: list[NotEvaluated],
) -> DistributionMetric:
    ordered = tuple(sorted(samples, key=lambda sample: sample.episode_id))
    values = [sample.value_ms for sample in ordered]
    return DistributionMetric(
        metric=metric,
        median_ms=lower_median(values) if values else None,
        p90_ms=nearest_rank_p90(values) if values else None,
        sample_count=len(values),
        samples=ordered,
        not_evaluated=tuple(
            sorted(not_evaluated, key=lambda item: (item.episode_id, item.reason))
        ),
        small_sample=len(values) < SMALL_N_THRESHOLD,
        methodology=TIME_METHODOLOGY,
    )


def _eligibility(bundle: EventBundle) -> EligibilitySection:
    episodes = bundle.episodes
    real = [item for item in episodes if item.episode_kind == "real"]
    denominator = [
        item for item in real if item.in_contract
    ]
    exclusions = tuple(
        sorted(
            (
                ExclusionEntry(
                    episode_id=item.episode_id,
                    episode_kind=item.episode_kind,
                    reason=item.exclusion_reason or "unspecified",
                )
                for item in episodes
                if not item.in_contract
            ),
            key=lambda entry: entry.episode_id,
        )
    )
    coverage = 100 * len(denominator) // len(real) if real else None
    return EligibilitySection(
        total_episodes=len(episodes),
        real_episodes=len(real),
        denominator_count=len(denominator),
        technical_episode_ids=tuple(
            sorted(
                item.episode_id
                for item in episodes
                if item.episode_kind == "technical"
            )
        ),
        synthetic_episode_ids=tuple(
            sorted(
                item.episode_id
                for item in episodes
                if item.episode_kind == "synthetic"
            )
        ),
        exclusions=exclusions,
        coverage_ratio_percent=coverage,
        hidden_episode_ids=tuple(
            sorted(_event_episode_ids(bundle) - {item.episode_id for item in episodes})
        ),
    )


def derive_report(bundle: EventBundle) -> MetricsReport:
    eligibility = _eligibility(bundle)
    denominator = tuple(
        episode
        for episode in bundle.episodes
        if episode.episode_kind == "real" and episode.in_contract
    )
    genuine = frozenset(
        episode.episode_id
        for episode in denominator
        if genuine_operator_approval(bundle, episode)
    )
    timing: TimingDerivation = derive_timing(bundle, denominator, genuine)

    metrics = [
        _distribution(
            phase,
            timing.phase_samples[phase],
            timing.phase_not_evaluated[phase],
        )
        for phase in TIMING_PHASES
    ]
    aht = _distribution(
        ACTIVE_HUMAN_TIME, timing.aht_samples, timing.aht_not_evaluated
    )
    metrics.append(aht)

    first_pass = first_pass_section(bundle, denominator, genuine)
    claims_review = tuple(
        evaluate_claim(
            claim,
            aht=aht,
            first_pass=first_pass,
            eligibility=eligibility,
        )
        for claim in bundle.claims
    )
    return MetricsReport(
        input_bindings=bundle.bindings,
        eligibility=eligibility,
        time_metrics=tuple(metrics),
        first_pass=first_pass,
        corrections=corrections_section(bundle),
        build=build_section(bundle),
        qc=qc_section(bundle),
        storage=storage_section(bundle),
        trace=trace_section(bundle),
        data_quality=tuple(
            sorted(
                timing.flags,
                key=lambda flag: (flag.episode_id, flag.flag, flag.detail),
            )
        ),
        claims_review=claims_review,
    )


__all__ = ["derive_report"]
