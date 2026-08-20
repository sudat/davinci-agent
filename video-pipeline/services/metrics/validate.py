"""The honesty gate: typed validation of a MetricsReport against its bundle.

Recomputes everything from the bundle (misleading numbers fail), re-hashes
every bound input (stale bundles fail), refuses human-time KPI samples
from non-eligible episodes (technical/synthetic fixtures never support
human-time claims), refuses real-episode activity without a genuine
operator approval chain, refuses hidden out-of-contract episodes, and
refuses broken Decision→Source→Review→Build traces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from services.foundation_io import canonical_model_bytes, sha256_file
from services.metrics.bundle import BUNDLE_FILES
from services.metrics.derive import derive_report
from services.metrics.honesty import (
    evaluate_claim,
    genuine_operator_approval,
    kpi_eligible_ids,
)
from services.metrics.models import ACTIVE_HUMAN_TIME
from services.metrics.report_models import (
    MetricsReport,
    ValidationFailure,
)

if TYPE_CHECKING:
    from services.metrics.bundle import EventBundle

_COMPARED_SECTIONS: tuple[str, ...] = (
    "eligibility",
    "time_metrics",
    "first_pass",
    "corrections",
    "build",
    "qc",
    "storage",
    "trace",
    "data_quality",
    "claims_review",
)


def _binding_failures(report: MetricsReport, bundle: EventBundle) -> list[ValidationFailure]:
    bound = {binding.name: binding.sha256 for binding in report.input_bindings}
    failures: list[ValidationFailure] = []
    for name in BUNDLE_FILES:
        path = bundle.root / name
        actual = sha256_file(path) if path.is_file() else None
        if name not in bound:
            failures.append(
                ValidationFailure(
                    code="stale_binding", detail=f"missing binding for {name}"
                )
            )
        elif bound[name] != actual:
            failures.append(
                ValidationFailure(
                    code="stale_binding",
                    detail=f"{name} changed after the report was derived "
                    f"(bound {bound[name]}, now {actual})",
                )
            )
    return failures


def _canonical(value: BaseModel | tuple[BaseModel, ...]) -> bytes:
    if isinstance(value, tuple):
        return b"[" + b",".join(canonical_model_bytes(item) for item in value) + b"]"
    return canonical_model_bytes(value)


def _drift_failures(report: MetricsReport, recomputed: MetricsReport) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []
    for section in _COMPARED_SECTIONS:
        reported: BaseModel | tuple[BaseModel, ...] = getattr(report, section)
        derived: BaseModel | tuple[BaseModel, ...] = getattr(recomputed, section)
        if _canonical(reported) != _canonical(derived):
            failures.append(
                ValidationFailure(
                    code="report_drift",
                    detail=f"section {section} differs from bundle re-derivation",
                )
            )
    return failures


def _sample_failures(report: MetricsReport, eligible: frozenset[str]) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []
    for metric in report.time_metrics:
        failures.extend(
            ValidationFailure(
                code="kpi_claim_unsupported",
                detail=(
                    f"{metric.metric}: human-time sample from non-eligible "
                    f"episode {sample.episode_id} (technical/synthetic and "
                    "unapproved episodes never support human-time claims)"
                ),
            )
            for sample in metric.samples
            if sample.episode_id not in eligible
        )
        if metric.sample_count == 0 and metric.median_ms is not None:
            failures.append(
                ValidationFailure(
                    code="kpi_claim_unsupported",
                    detail=f"{metric.metric}: median claimed with zero samples",
                )
            )
    return failures


def _claim_failures(recomputed: MetricsReport, bundle: EventBundle) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []
    aht = next(
        metric
        for metric in recomputed.time_metrics
        if metric.metric == ACTIVE_HUMAN_TIME
    )
    for claim in bundle.claims:
        review = evaluate_claim(
            claim, aht=aht, first_pass=recomputed.first_pass, eligibility=recomputed.eligibility
        )
        if not review.supported:
            failures.append(
                ValidationFailure(
                    code="kpi_claim_unsupported", detail=f"claim {review.claim_id}: {review.detail}"
                )
            )
    return failures


def _approval_failures(bundle: EventBundle) -> list[ValidationFailure]:
    failures: list[ValidationFailure] = []
    timing_ids = {event.episode_id for event in bundle.timing_events}
    for episode in bundle.episodes:
        if episode.episode_kind != "real" or not episode.in_contract:
            continue
        genuine = genuine_operator_approval(bundle, episode)
        if episode.editorial_approval_record_id is not None and not genuine:
            failures.append(
                ValidationFailure(
                    code="false_real_approval",
                    detail=(
                        f"episode {episode.episode_id}: declared approval record is "
                        "not a genuine operator editorial approval"
                    ),
                )
            )
        elif episode.episode_id in timing_ids and not genuine:
            failures.append(
                ValidationFailure(
                    code="false_real_approval",
                    detail=(
                        f"episode {episode.episode_id}: human timing events recorded "
                        "without any genuine operator approval record"
                    ),
                )
            )
    return failures


def _hidden_failures(recomputed: MetricsReport) -> list[ValidationFailure]:
    return [
        ValidationFailure(
            code="hidden_out_of_contract",
            detail=(
                f"episode {episode_id} appears in event streams but is not declared; "
                "out-of-contract episodes must be excluded openly, never hidden"
            ),
        )
        for episode_id in recomputed.eligibility.hidden_episode_ids
    ]


def _trace_failures(recomputed: MetricsReport) -> list[ValidationFailure]:
    if recomputed.trace.status != "broken":
        return []
    return [
        ValidationFailure(
            code="broken_trace",
            detail=(
                f"Decision→Source→Review→Build trace broken with "
                f"{len(recomputed.trace.breaks)} breaks"
            ),
        )
    ]


def validate_report(report: MetricsReport, bundle: EventBundle) -> tuple[ValidationFailure, ...]:
    recomputed = derive_report(bundle)
    failures: list[ValidationFailure] = []
    failures += _binding_failures(report, bundle)
    failures += _drift_failures(report, recomputed)
    failures += _sample_failures(report, kpi_eligible_ids(bundle))
    failures += _claim_failures(recomputed, bundle)
    failures += _approval_failures(bundle)
    failures += _hidden_failures(recomputed)
    failures += _trace_failures(recomputed)
    return tuple(failures)


__all__ = ["validate_report"]
