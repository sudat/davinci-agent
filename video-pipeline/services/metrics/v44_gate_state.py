"""v44 operator-gated gate state — blocked/observation/summary records.

``V44GateBlockedV1`` records a gate that could not run because operator
inputs were unavailable — never simulated, never passed. ``V1ObservationRecord``
is the strict ``v44-1-observation-v1`` evidence the V44-1 observer derives
read-only from a cockpit episode workspace (see ``services.cli.v44_observe``).
``V44GateSummaryV1`` (``v44-gate-summary-v1``, T16) is the V44-2 gate
summary whose ``passed: true`` state is UNREPRESENTABLE unless every
required evidence field is present (plan T16 anti-fabrication).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes
from services.metrics.v44_baseline import (
    CommitSha40,  # noqa: TC001 (pydantic resolves it at class build)
)


def _to_tuple(value: object) -> object:
    """Coerce a JSON array to a tuple (StrictModel strict mode rejects lists)."""
    if isinstance(value, list):
        return tuple(value)
    return value


class V44GateBlockedV1(StrictModel):
    """A blocked operator-gated run (``v44-gate-blocked-v1``).

    ``missing`` must itemize at least one absent operator input — a blocked
    record with nothing missing is unrepresentable. ``reason`` is additive:
    ``operator-needed`` (operator inputs unavailable) and, since Task 3,
    ``asr-alignment-failed`` (the system_asr transcript failed the four
    predeclared alignment thresholds before any paid call). Historical
    blocked files carrying only ``operator-needed`` parse unchanged.
    """

    schema_version: Literal["v44-gate-blocked-v1"] = "v44-gate-blocked-v1"
    gate: Literal["V44-0", "V44-1", "V44-2"]
    reason: Literal["operator-needed", "asr-alignment-failed"]
    missing: Annotated[tuple[str, ...], BeforeValidator(_to_tuple), Field(min_length=1)]
    operator_instructions: Annotated[str, Field(min_length=1, strict=True)]
    checked_at: Annotated[str, Field(min_length=1, strict=True)]
    commit_sha: CommitSha40


def write_blocked_record(path: Path, record: V44GateBlockedV1) -> None:
    """Persist a blocked record as canonical JSON (atomic write)."""
    atomic_write(path, canonical_model_bytes(record))


# ---------------------------------------------------------------------------
# V44-1 observation record — schema ``v44-1-observation-v1``
#
# The observer (``services.cli.v44_observe``) polls the cockpit episode
# workspace read-only, snapshots the stage timeline, derives TTFRP from
# intake→preview mtimes, and records correction/rebuild timings. The
# ``internal_path_leak`` flag is computed: any captured operator chat
# text containing ``"jobs/"``/``"artifacts/"``/``".json"``. Seeded
# state is refused BEFORE this model is constructed.
# ---------------------------------------------------------------------------


class ObservationStageRunV1(StrictModel):
    stage_name: Identifier
    status: Literal["pending", "running", "succeeded", "failed_blocked", "failed_retryable"]
    retry_count: int = Field(ge=0, strict=True)
    idempotency_key: Identifier


class ObservationStageTimelineV1(StrictModel):
    job_status: Literal[
        "CREATED",
        "INGESTED",
        "NORMALIZED",
        "ANALYZED",
        "PLAN_PROPOSED",
        "PLAN_COMMITTED",
        "PREVIEW_READY",
        "EDITORIAL_APPROVED",
        "RESOLVE_BUILT",
        "QC_PASSED",
        "FINAL_APPROVED",
        "FROZEN",
    ]
    current_stage: Identifier
    runs: Annotated[tuple[ObservationStageRunV1, ...], BeforeValidator(_to_tuple)]


class ObservationCorrectionV1(StrictModel):
    """One NL correction — ``applied`` is episode-level evidence.

    The cockpit persists NL text in ``review-chat.jsonl`` and the
    structured command (without text) in ``applied-commands.jsonl`` —
    per-correction correlation is not persisted, so ``applied`` is
    uniform: True iff ≥1 non-deferred AppliedCommand exists. Likewise
    ``rebuild_wall_clock_seconds`` is the latest rebuild metric's wall
    time (same caveat).
    """

    text: Annotated[str, Field(min_length=1, strict=True)]
    at_seconds: Annotated[float, Field(ge=0.0, strict=True)] | None = None
    applied: bool
    rebuild_wall_clock_seconds: Annotated[float, Field(ge=0.0, strict=True)] | None = None


class ObservationRebuildV1(StrictModel):
    sequence: int = Field(ge=1, strict=True)
    applied_command: Identifier
    stages: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)]
    rebuild_wall_clock_seconds: Annotated[float, Field(ge=0.0, strict=True)]
    unrelated_stages_skipped: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)]
    at: Annotated[str, Field(min_length=1, strict=True)]
    interpretation_ms: Annotated[float, Field(ge=0.0, strict=True)] | None = None


class V1ObservationRecord(StrictModel):
    """Strict ``v44-1-observation-v1`` — the V44-1 cockpit observation."""

    schema_version: Literal["v44-1-observation-v1"] = "v44-1-observation-v1"
    episode_id: Identifier
    stage_timeline: ObservationStageTimelineV1
    ttfrp_seconds: Annotated[float, Field(ge=0.0, strict=True)] | None = None
    corrections: Annotated[tuple[ObservationCorrectionV1, ...], BeforeValidator(_to_tuple)]
    rebuild_records: Annotated[tuple[ObservationRebuildV1, ...], BeforeValidator(_to_tuple)]
    operator_note: Annotated[str, Field(min_length=1, strict=True)] | None = None
    internal_path_leak: bool
    observed_at: Annotated[str, Field(min_length=1, strict=True)]


def write_observation_record(path: Path, record: V1ObservationRecord) -> None:
    """Persist an observation record as canonical JSON (atomic write)."""
    atomic_write(path, canonical_model_bytes(record))


# ---------------------------------------------------------------------------
# V44-2 gate summary — schema ``v44-gate-summary-v1`` (T16)
#
# The summary DERIVES from real run evidence (finishing report +
# publishability record + bootstrap time log). ``passed: true`` is
# unrepresentable at the schema level unless: the operator verdict is a
# publishable* verdict, zero domains are blocked, technical QC passed,
# editorial QC has zero blocked items, bootstrap AHT + direct-Resolve
# minutes are numeric, and director/evidence pins are recorded. This is
# the structural anti-fabrication guarantee (plan T16 acceptance (c)).
# ---------------------------------------------------------------------------

OperatorVerdict = Literal["publishable", "publishable_after_fixes", "not_publishable"]

_QcVerdict = Literal["passed", "blocked"]


def _pass_evidence_problems(summary: V44GateSummaryV1) -> list[str]:
    """Every ``passed=true`` evidence requirement, as (holds, message) rows."""

    checks: tuple[tuple[bool, str], ...] = (
        (
            summary.operator_verdict in ("publishable", "publishable_after_fixes"),
            f"operator_verdict must be publishable*, got {summary.operator_verdict!r}",
        ),
        (
            not summary.blocked_domains,
            f"blocked domains present: {list(summary.blocked_domains)}",
        ),
        (
            summary.technical_qc == "passed",
            f"technical_qc must be 'passed', got {summary.technical_qc!r}",
        ),
        (
            summary.editorial_qc_blocked_items == 0,
            f"editorial_qc_blocked_items must be 0, got {summary.editorial_qc_blocked_items!r}",
        ),
        (summary.bootstrap_aht_minutes is not None, "bootstrap_aht_minutes is required"),
        (
            summary.direct_resolve_minutes is not None,
            "direct_resolve_minutes is required",
        ),
        (summary.director_pin_model is not None, "director_pin_model is required"),
        (bool(summary.evidence_pins), "evidence_pins must be non-empty"),
    )
    return [message for holds, message in checks if not holds]


class V44GateSummaryV1(StrictModel):
    """Strict ``v44-gate-summary-v1`` — the V44-2 gate summary.

    ``passed=False`` summaries are always representable (an honest failed
    or incomplete gate); only ``passed=True`` requires the full evidence
    set below. ``editorial_qc_blocked_items`` is the finishing report's
    editorial-QC critical count (T13 ``FinishingEditorialQcBlockV1``).
    """

    schema_version: Literal["v44-gate-summary-v1"] = "v44-gate-summary-v1"
    gate: Literal["V44-2"] = "V44-2"
    passed: bool
    operator_verdict: OperatorVerdict | None = None
    blocked_domains: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = ()
    technical_qc: _QcVerdict | None = None
    editorial_qc_blocked_items: int | None = Field(default=None, ge=0, strict=True)
    bootstrap_aht_minutes: float | None = Field(default=None, ge=0.0, strict=True)
    direct_resolve_minutes: float | None = Field(default=None, ge=0.0, strict=True)
    director_pin_model: str | None = Field(default=None, min_length=1, strict=True)
    evidence_pins: dict[str, str] = Field(default_factory=dict)
    finishing_report_ref: str | None = Field(default=None, min_length=1, strict=True)
    subtitle_proof_ref: str | None = Field(default=None, min_length=1, strict=True)
    artifacts: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = ()
    recorded_at: Annotated[str, Field(min_length=1, strict=True)]
    commit_sha: CommitSha40

    @model_validator(mode="after")
    def require_full_evidence_when_passed(self) -> V44GateSummaryV1:
        if not self.passed:
            return self
        problems = _pass_evidence_problems(self)
        if problems:
            raise PydanticCustomError(
                "passed_without_full_evidence",
                "passed=true requires complete evidence: {problems}",
                {"problems": "; ".join(problems)},
            )
        return self


def write_v44_2_summary(path: Path, record: V44GateSummaryV1) -> None:
    """Persist a V44-2 gate summary as canonical JSON (atomic write)."""
    atomic_write(path, canonical_model_bytes(record))


__all__ = [
    "ObservationCorrectionV1",
    "ObservationRebuildV1",
    "ObservationStageRunV1",
    "ObservationStageTimelineV1",
    "OperatorVerdict",
    "V1ObservationRecord",
    "V44GateBlockedV1",
    "V44GateSummaryV1",
    "write_blocked_record",
    "write_observation_record",
    "write_v44_2_summary",
]
