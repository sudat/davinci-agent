"""MetricsReport v1: the auditable outcome report and its honesty types.

Every numeric section is derived from a hash-bound event bundle; the
validation codes are the honesty gate's typed failures.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.metrics.models import ACTIVE_HUMAN_TIME, EpisodeKind, Seq, TimingPhase
from services.metrics.stats import TIME_METHODOLOGY

MetricName = TimingPhase | Literal["active_human_time"]

ValidationFailureCode = Literal[
    "stale_binding",
    "report_drift",
    "kpi_claim_unsupported",
    "false_real_approval",
    "hidden_out_of_contract",
    "broken_trace",
]

KPI_SAMPLE_NOTE = (
    " samples eligible only for episode_kind=real, in_contract=true, with a genuine "
    "operator editorial approval record; technical/synthetic fixtures never contribute."
)


class BundleFileBinding(StrictModel):
    name: str = Field(min_length=1, strict=True)
    sha256: Sha256 | None


class NotEvaluated(StrictModel):
    episode_id: Identifier
    reason: str = Field(min_length=1, strict=True)


class TimeSample(StrictModel):
    episode_id: Identifier
    value_ms: int = Field(ge=0, strict=True)


class DistributionMetric(StrictModel):
    metric: MetricName
    median_ms: int | None = Field(default=None, ge=0, strict=True)
    p90_ms: int | None = Field(default=None, ge=0, strict=True)
    sample_count: int = Field(ge=0, strict=True)
    samples: Seq[TimeSample] = ()
    not_evaluated: Seq[NotEvaluated] = ()
    small_sample: bool
    methodology: str = TIME_METHODOLOGY + KPI_SAMPLE_NOTE

    @model_validator(mode="after")
    def require_count_consistency(self) -> DistributionMetric:
        if self.sample_count != len(self.samples):
            raise PydanticCustomError(
                "sample_count", "sample_count must equal the number of samples"
            )
        if (self.median_ms is None) != (self.sample_count == 0):
            raise PydanticCustomError(
                "median_absent", "median_ms is present iff sample_count > 0"
            )
        if (self.p90_ms is None) != (self.sample_count == 0):
            raise PydanticCustomError(
                "p90_absent", "p90_ms is present iff sample_count > 0"
            )
        return self


class ExclusionEntry(StrictModel):
    episode_id: Identifier
    episode_kind: EpisodeKind
    reason: str = Field(min_length=1, strict=True)


class EligibilitySection(StrictModel):
    total_episodes: int = Field(ge=0, strict=True)
    real_episodes: int = Field(ge=0, strict=True)
    denominator_count: int = Field(ge=0, strict=True)
    technical_episode_ids: Seq[Identifier] = ()
    synthetic_episode_ids: Seq[Identifier] = ()
    exclusions: Seq[ExclusionEntry] = ()
    coverage_ratio_percent: int | None = Field(default=None, ge=0, le=100, strict=True)
    hidden_episode_ids: Seq[Identifier] = ()


class FirstPassSection(StrictModel):
    denominator_count: int = Field(ge=0, strict=True)
    first_pass_count: int = Field(ge=0, strict=True)
    rate_percent: int | None = Field(default=None, ge=0, le=100, strict=True)
    not_evaluated: Seq[NotEvaluated] = ()


class CorrectionsSection(StrictModel):
    total_corrections: int = Field(ge=0, strict=True)
    episodes_with_corrections: int = Field(ge=0, strict=True)
    max_per_episode: int = Field(ge=0, strict=True)


class BuildSection(StrictModel):
    stages_observed: Seq[str] = ()
    attempt_succeeded: int = Field(ge=0, strict=True)
    attempt_failed: int = Field(ge=0, strict=True)
    run_blocked: int = Field(ge=0, strict=True)
    run_reused: int = Field(ge=0, strict=True)
    run_recovered: int = Field(ge=0, strict=True)


class QcSection(StrictModel):
    reports: int = Field(ge=0, strict=True)
    passed: int = Field(ge=0, strict=True)
    blocked: int = Field(ge=0, strict=True)
    blocker_issues: int = Field(ge=0, strict=True)
    major_issues: int = Field(ge=0, strict=True)
    minor_issues: int = Field(ge=0, strict=True)


class StorageSection(StrictModel):
    total_bytes: int = Field(ge=0, strict=True)
    authoritative_bytes: int = Field(ge=0, strict=True)
    rebuildable_bytes: int = Field(ge=0, strict=True)
    runtime_cache_bytes: int = Field(ge=0, strict=True)
    manual_finalization_bytes: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_total_matches_classes(self) -> StorageSection:
        summed = (
            self.authoritative_bytes
            + self.rebuildable_bytes
            + self.runtime_cache_bytes
            + self.manual_finalization_bytes
        )
        if summed != self.total_bytes:
            raise PydanticCustomError(
                "storage_total", "total_bytes must equal the sum of class bytes"
            )
        return self


class TraceBreak(StrictModel):
    code: str = Field(min_length=1, strict=True)
    detail: str = Field(min_length=1, strict=True)


class TraceSection(StrictModel):
    status: Literal["complete", "broken", "not_evaluated"]
    decision_count: int = Field(ge=0, strict=True)
    build_item_count: int = Field(ge=0, strict=True)
    review_linked_decisions: int = Field(ge=0, strict=True)
    breaks: Seq[TraceBreak] = ()

    @model_validator(mode="after")
    def require_breaks_iff_broken(self) -> TraceSection:
        if self.status == "broken" and not self.breaks:
            raise PydanticCustomError(
                "trace_breaks", "broken traces must list at least one break"
            )
        if self.status != "broken" and self.breaks:
            raise PydanticCustomError(
                "trace_breaks", "only broken traces carry breaks"
            )
        return self


class DataQualityFlag(StrictModel):
    episode_id: Identifier
    flag: Literal["idle_gap", "self_report_gap", "log_gap", "missing_timestamp"]
    detail: str = Field(min_length=1, strict=True)


class ClaimReview(StrictModel):
    claim_id: Identifier
    supported: bool
    detail: str = Field(min_length=1, strict=True)


class ValidationFailure(StrictModel):
    code: ValidationFailureCode
    detail: str = Field(min_length=1, strict=True)


class ReportValidation(StrictModel):
    valid: bool
    failures: Seq[ValidationFailure] = ()

    @model_validator(mode="after")
    def require_consistency(self) -> ReportValidation:
        if self.valid and self.failures:
            raise PydanticCustomError(
                "validation_consistency", "valid reports carry no failures"
            )
        if not self.valid and not self.failures:
            raise PydanticCustomError(
                "validation_consistency", "invalid reports must list failures"
            )
        return self


class MetricsReport(StrictModel):
    schema_version: Literal["metrics-report-v1"] = "metrics-report-v1"
    input_bindings: Seq[BundleFileBinding]
    eligibility: EligibilitySection
    time_metrics: Seq[DistributionMetric] = ()
    first_pass: FirstPassSection
    corrections: CorrectionsSection
    build: BuildSection
    qc: QcSection
    storage: StorageSection
    trace: TraceSection
    data_quality: Seq[DataQualityFlag] = ()
    claims_review: Seq[ClaimReview] = ()
    validation: ReportValidation = ReportValidation(valid=True)


__all__ = [
    "ACTIVE_HUMAN_TIME",
    "BuildSection",
    "BundleFileBinding",
    "ClaimReview",
    "CorrectionsSection",
    "DataQualityFlag",
    "DistributionMetric",
    "EligibilitySection",
    "ExclusionEntry",
    "FirstPassSection",
    "MetricName",
    "MetricsReport",
    "NotEvaluated",
    "QcSection",
    "ReportValidation",
    "StorageSection",
    "TimeSample",
    "TraceBreak",
    "TraceSection",
    "ValidationFailure",
    "ValidationFailureCode",
]
