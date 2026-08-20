"""First-pass, correction, build, QC, and storage aggregate sections."""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.metrics.report_models import (
    BuildSection,
    CorrectionsSection,
    FirstPassSection,
    NotEvaluated,
    QcSection,
    StorageSection,
)
from services.metrics.streams import is_correction_event

if TYPE_CHECKING:
    from services.metrics.bundle import EventBundle
    from services.metrics.models import EpisodeDeclaration


def correction_counts(bundle: EventBundle) -> dict[str, int]:
    counts: dict[str, int] = {}
    for wrapped in bundle.review_events:
        if is_correction_event(wrapped):
            counts[wrapped.episode_id] = counts.get(wrapped.episode_id, 0) + 1
    return counts


def first_pass_section(
    bundle: EventBundle,
    denominator: tuple[EpisodeDeclaration, ...],
    genuine: frozenset[str],
) -> FirstPassSection:
    if not bundle.review_events:
        return FirstPassSection(
            denominator_count=len(denominator),
            first_pass_count=0,
            rate_percent=None,
            not_evaluated=tuple(
                NotEvaluated(episode_id=episode.episode_id, reason="no review event stream")
                for episode in denominator
            ),
        )
    counts = correction_counts(bundle)
    first_pass = 0
    not_evaluated: list[NotEvaluated] = []
    for episode in denominator:
        if episode.episode_id not in genuine:
            not_evaluated.append(
                NotEvaluated(
                    episode_id=episode.episode_id,
                    reason="no genuine operator approval record",
                )
            )
        elif counts.get(episode.episode_id, 0) == 0:
            first_pass += 1
    rate = 100 * first_pass // len(denominator) if denominator else None
    return FirstPassSection(
        denominator_count=len(denominator),
        first_pass_count=first_pass,
        rate_percent=rate,
        not_evaluated=tuple(not_evaluated),
    )


def corrections_section(bundle: EventBundle) -> CorrectionsSection:
    counts = correction_counts(bundle)
    return CorrectionsSection(
        total_corrections=sum(counts.values()),
        episodes_with_corrections=len(counts),
        max_per_episode=max(counts.values(), default=0),
    )


def build_section(bundle: EventBundle) -> BuildSection:
    counts = {
        "attempt-succeeded": 0,
        "attempt-failed": 0,
        "run-blocked": 0,
        "run-reused": 0,
        "run-recovered": 0,
    }
    stages: set[str] = set()
    for wrapped in bundle.stage_events:
        event = wrapped.event
        if event.kind in counts:
            counts[event.kind] += 1
        stages.add(event.stage_name)
    return BuildSection(
        stages_observed=tuple(sorted(stages)),
        attempt_succeeded=counts["attempt-succeeded"],
        attempt_failed=counts["attempt-failed"],
        run_blocked=counts["run-blocked"],
        run_reused=counts["run-reused"],
        run_recovered=counts["run-recovered"],
    )


def qc_section(bundle: EventBundle) -> QcSection:
    outcomes = bundle.qc_outcomes
    return QcSection(
        reports=len(outcomes),
        passed=sum(1 for outcome in outcomes if outcome.verdict == "passed"),
        blocked=sum(1 for outcome in outcomes if outcome.verdict == "blocked"),
        blocker_issues=sum(outcome.blocker_count for outcome in outcomes),
        major_issues=sum(outcome.major_count for outcome in outcomes),
        minor_issues=sum(outcome.minor_count for outcome in outcomes),
    )


def storage_section(bundle: EventBundle) -> StorageSection:
    totals = {
        "authoritative": 0,
        "rebuildable": 0,
        "runtime_cache": 0,
        "manual_finalization": 0,
    }
    for record in bundle.inventory:
        totals[record.retention_class] += record.byte_size
    return StorageSection(
        total_bytes=sum(totals.values()),
        authoritative_bytes=totals["authoritative"],
        rebuildable_bytes=totals["rebuildable"],
        runtime_cache_bytes=totals["runtime_cache"],
        manual_finalization_bytes=totals["manual_finalization"],
    )


__all__ = [
    "build_section",
    "correction_counts",
    "corrections_section",
    "first_pass_section",
    "qc_section",
    "storage_section",
]
