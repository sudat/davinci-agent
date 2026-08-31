"""Strict models for the T13 finishing harness (runtime reports only).

``FinishingRunReportV1`` (schema ``v44-finishing-run-v1``) is a RUNTIME
report written to ``<episode-root>/finishing/finishing-run.json`` — NOT a
new authoritative artifact (PRD v4.4 §0.2 cap: EditorialGroundTruthV1 +
ProductProofReportV1 only). It carries every quality domain's status +
justification, the QC verdicts (real or null-with-reason — never
fabricated), the executor identity, model/toolchain pins, and wall clock.

``TimeLogLineV1`` (schema ``v44-time-log-v1``) is one append-only
``time-log.jsonl`` line; the ``label`` is FIXED to ``bootstrap`` (PRD
§19.4: bootstrap and steady_state labels must never mix — the first
publish episode is bootstrap by definition).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel, to_tuple

#: The seven PRD 14.3 domains, in canonical report order (quality_domains).
REPORT_NAME: str = "finishing-run.json"

#: The seven PRD 14.3 domains, in canonical report order (quality_domains).
QUALITY_DOMAIN_NAMES: tuple[str, ...] = (
    "editorial_construction",
    "subtitle",
    "audio_finishing",
    "color_finishing",
    "framing_motion",
    "graphics_presentation",
    "delivery_qc",
)

TimeLogPhase = Literal[
    "ordinary_review",
    "kit_bootstrap",
    "taste_calibration",
    "troubleshooting",
    "direct_resolve",
]


_StrTuple = Annotated[tuple[str, ...], BeforeValidator(to_tuple)]


class FinishingKitSelectionV1(StrictModel):
    """Snapshot of one consumed kit-selection entry (latest per domain)."""

    domain: str = Field(min_length=1, strict=True)
    recipe_id: str | None = None
    semantic_intent: str | None = None


class FinishingQcBlockV1(StrictModel):
    """Technical QC verdict — real when run, null-with-reason otherwise.

    A fabricated verdict is unrepresentable: ``verdict`` set requires the
    measured counts + the report path; ``verdict`` null requires a
    non-blank reason naming why QC did not run.
    """

    verdict: Literal["passed", "blocked"] | None = None
    issue_count: int | None = Field(default=None, ge=0, strict=True)
    blocker_count: int | None = Field(default=None, ge=0, strict=True)
    unresolved_gate_count: int | None = Field(default=None, ge=0, strict=True)
    report_path: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def require_verdict_xor_reason(self) -> FinishingQcBlockV1:
        if self.verdict is None:
            if not (self.reason and self.reason.strip()):
                raise PydanticCustomError(
                    "qc_reason_required", "a null verdict requires a reason"
                )
            if self.issue_count is not None:
                raise PydanticCustomError(
                    "qc_counts_without_verdict", "counts require a real verdict"
                )
        elif self.reason is not None:
            raise PydanticCustomError(
                "qc_reason_only_when_skipped", "reason is only meaningful when skipped"
            )
        elif (
            self.issue_count is None
            or self.blocker_count is None
            or self.unresolved_gate_count is None
            or self.report_path is None
        ):
            raise PydanticCustomError(
                "qc_verdict_requires_measurements",
                "a verdict requires issue/blocker/gate counts and the report path",
            )
        return self


class FinishingEditorialQcBlockV1(StrictModel):
    """Editorial QC always runs (no media needed); counts + report path."""

    candidate_count: int = Field(ge=0, strict=True)
    critical_count: int = Field(ge=0, strict=True)
    needs_review_count: int = Field(ge=0, strict=True)
    report_path: str = Field(min_length=1, strict=True)


class NativeRenderBlockV1(StrictModel):
    """DaVinci-native render lifecycle proof (Task 7).

    The finishing CLI binds ``final_preview_path`` to this output only after
    the Resolve job reported 100%/not-rendering and the file passed media QC.
    Fake/external remux files never produce this block.
    """

    job_id: str = Field(min_length=1, strict=True)
    output_path: str = Field(min_length=1, strict=True)
    output_sha256: str = Field(min_length=64, max_length=64, strict=True)
    output_size_bytes: int = Field(ge=1, strict=True)
    duration_seconds: float = Field(gt=0, strict=True)
    video_codec: str = Field(min_length=1, strict=True)
    width: int = Field(ge=1, strict=True)
    height: int = Field(ge=1, strict=True)
    audio_codec: str = Field(min_length=1, strict=True)
    audio_channels: int = Field(ge=1, strict=True)


class FinishingRunReportV1(StrictModel):
    """The V44-2 finishing run report (runtime; NOT authoritative)."""

    schema_version: Literal["v44-finishing-run-v1"]
    episode_id: Identifier
    run_id: str = Field(min_length=1, strict=True)
    executor: Literal["fake", "live"]
    executor_note: str = Field(min_length=1, strict=True)
    director_model_id: str = Field(min_length=1, strict=True)
    analysis_provider: str = Field(min_length=1, strict=True)
    review_head_version: int = Field(ge=1, strict=True)
    kit_selections: Annotated[
        tuple[FinishingKitSelectionV1, ...], BeforeValidator(to_tuple)
    ]
    plan_compiled: bool
    plan_id: str | None = None
    plan_step_count: int | None = Field(default=None, ge=0, strict=True)
    execution_outcome: Literal["completed", "failed"] | None = None
    completed_step_count: int | None = Field(default=None, ge=0, strict=True)
    failed_step_count: int | None = Field(default=None, ge=0, strict=True)
    fallback_rung_count: int | None = Field(default=None, ge=0, strict=True)
    domain_statuses: dict[str, str]
    domain_justifications: dict[str, str]
    blocked_domains: _StrTuple = ()
    surfaced_manual_items: _StrTuple = ()
    gate_decision: Literal["pass", "reject"]
    technical_qc: FinishingQcBlockV1
    editorial_qc: FinishingEditorialQcBlockV1
    final_preview_path: str = Field(min_length=1, strict=True)
    final_preview_sha256: str = Field(min_length=1, strict=True)
    native_render: NativeRenderBlockV1 | None = None
    wall_clock_seconds: float = Field(ge=0.0, strict=True)
    notes: _StrTuple = ()

    @model_validator(mode="after")
    def require_compiled_parity(self) -> FinishingRunReportV1:
        execution_fields = (
            self.plan_id,
            self.plan_step_count,
            self.execution_outcome,
            self.completed_step_count,
            self.failed_step_count,
            self.fallback_rung_count,
        )
        if self.plan_compiled and any(field is None for field in execution_fields):
            raise PydanticCustomError(
                "compiled_requires_execution",
                "a compiled run records its plan id and execution counts",
            )
        if not self.plan_compiled and any(field is not None for field in execution_fields):
            raise PydanticCustomError(
                "execution_without_compile",
                "execution fields are only meaningful on a compiled run",
            )
        return self

    @model_validator(mode="after")
    def require_all_seven_domains(self) -> FinishingRunReportV1:
        expected = set(QUALITY_DOMAIN_NAMES)
        if set(self.domain_statuses) != expected:
            raise PydanticCustomError(
                "domain_set_mismatch",
                "domain_statuses must carry exactly the seven quality domains",
            )
        for domain, status in self.domain_statuses.items():
            if status == "applied":
                if domain in self.domain_justifications:
                    raise PydanticCustomError(
                        "justification_only_for_non_applied",
                        "applied domain {domain} carries a justification",
                        {"domain": domain},
                    )
            elif domain not in self.domain_justifications:
                raise PydanticCustomError(
                    "justification_required",
                    "non-applied domain {domain} records its justification",
                    {"domain": domain},
                )
        return self


class TimeLogLineV1(StrictModel):
    """One ``time-log.jsonl`` line (PRD §19.4; bootstrap label is fixed)."""

    schema_version: Literal["v44-time-log-v1"]
    phase: TimeLogPhase
    minutes: float = Field(gt=0.0, strict=True)
    label: Literal["bootstrap"]
    at: str = Field(min_length=1, strict=True)


#: The five PRD §19.4 bootstrap active-human categories, canonical order.
TIME_LOG_PHASES: Final[tuple[TimeLogPhase, ...]] = (
    "ordinary_review",
    "kit_bootstrap",
    "taste_calibration",
    "troubleshooting",
    "direct_resolve",
)


@dataclass(frozen=True, slots=True)
class TimeLogTotals:
    """The five-category bootstrap AHT summary (plan T8).

    ``aht_minutes`` is non-null ONLY when every one of the five canonical
    phases (``TIME_LOG_PHASES``) has at least one recorded entry — a
    missing phase is missing time, never an estimated zero, so a partial
    log keeps AHT null and fails Gate V44-2. ``direct_resolve_minutes``
    reports the recorded direct-Resolve subset independently (still never
    added into AHT a second time). A log with zero lines stays ``None``
    for both.
    """

    phase_minutes: Mapping[TimeLogPhase, float]
    aht_minutes: float | None
    direct_resolve_minutes: float | None


def summarize_time_log(lines: Sequence[TimeLogLineV1]) -> TimeLogTotals:
    """AHT only from a complete five-phase log; direct Resolve as subset."""

    per_phase: dict[TimeLogPhase, float] = {}
    for line in lines:
        per_phase[line.phase] = per_phase.get(line.phase, 0.0) + float(line.minutes)
    complete = all(phase in per_phase for phase in TIME_LOG_PHASES)
    return TimeLogTotals(
        phase_minutes=per_phase,
        aht_minutes=float(sum(per_phase.values())) if complete else None,
        direct_resolve_minutes=per_phase.get("direct_resolve"),
    )


__all__ = [
    "QUALITY_DOMAIN_NAMES",
    "REPORT_NAME",
    "TIME_LOG_PHASES",
    "FinishingEditorialQcBlockV1",
    "FinishingKitSelectionV1",
    "FinishingQcBlockV1",
    "FinishingRunReportV1",
    "NativeRenderBlockV1",
    "TimeLogLineV1",
    "TimeLogPhase",
    "TimeLogTotals",
    "summarize_time_log",
]
