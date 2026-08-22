"""Three-episode measurement harness (Phase 6 / Gate V43-4b, plan task 52).

``EpisodeRunReportV1`` aggregates implementation-plan §10.4's full
per-episode measurement field list for the Episode A/B/C validation runs.
Fields are operator-fillable with EXPLICIT nulls: ``collect_episode_run``
auto-fills what episode run artifacts carry (episode0-report-v1,
quality-domain-report-v1, interruption summary, plus editorial QC and the
task-54 upload ledger as evidence refs) and names every remaining null as a
coverage gap. ``aggregate_three_episodes`` folds three reports into the
Phase-6 exit-criteria summary (non-talking-head failure zero, automation
rate vs the 80% target context). ``check_footage_sources`` is the pre-start
decision point: absent Episode A/C footage yields a recorded synthetic
screen-capture fallback PROPOSAL — never an indefinite wait.

This module never claims the 30-minute Active-Human-Time target: Phase 6
exists for product-scope validation only (impl-plan §10.4 final note).

Derivation notes (deterministic, no LLM, no fabrication):
- total_source_minutes comes only from a probed episode0 manifest duration.
- blocking_defects counts quality domains whose status is ``blocked``;
  manual_fallback_capabilities names domains that are
  ``manual_fallback_required`` (the finishing report is the sole source).
- interruption_counts mirrors services/episode_cockpit/interruption_policy
  ``InterruptionSummary``; the level vocabulary is duplicated locally so
  importing this metrics module never pulls the FastAPI cockpit app.
- The task-54 upload ledger and task-41 editorial QC report are detected by
  shape/schema sniffing and cited as evidence only — their numbers are NOT
  corrections or operator measurements and never populate fields.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Final, Literal, get_args

from pydantic import BeforeValidator, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel
from services.creative_plan.quality_domains import (
    QUALITY_DOMAINS,
    QualityDomainReportV1,
    QualityDomainStatus,
)
from services.foundation_io import atomic_write, canonical_model_bytes
from services.metrics.episode0_baseline import Episode0ReportV1

# allow: SIZE_OK — plan task 52 pins the harness to this single module
# (schema + collector + aggregator + footage decision; T39/T40/T41 precedent).

EPISODE_RUN_REPORT_SCHEMA: Final = "episode-run-report-v1"
PHASE6_SUMMARY_SCHEMA: Final = "phase6-summary-v1"
FOOTAGE_DECISION_SCHEMA: Final = "footage-decision-v1"
DEFAULT_AUTOMATION_TARGET: Final = 0.8

EpisodeClassName = Literal["a_talking_broll", "b_visual_first", "c_mixed"]
EPISODE_CLASSES: tuple[EpisodeClassName, ...] = (
    "a_talking_broll",
    "b_visual_first",
    "c_mixed",
)

#: Mirrors services.episode_cockpit.interruption_policy.InterruptionLevel
#: (kept local: episode_cockpit's __init__ imports the FastAPI app).
InterruptionLevelName = Literal[
    "SAFE_AUTO_RESOLVE", "DEGRADED_BUT_RECOVERABLE", "HUMAN_DECISION_REQUIRED"
]
INTERRUPTION_LEVELS: tuple[InterruptionLevelName, ...] = (
    "SAFE_AUTO_RESOLVE",
    "DEGRADED_BUT_RECOVERABLE",
    "HUMAN_DECISION_REQUIRED",
)

UploadStatusName = Literal["started", "completed", "failed"]
TargetRate = Annotated[float, Field(gt=0, le=1, strict=True)]


class EpisodeRunReportError(ValueError):
    """Typed refusal from the measurement harness (never silent)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _coerce_float(value: object) -> object:
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


StrSeq = Annotated[tuple[str, ...], BeforeValidator(_to_tuple)]
NonNegativeFloat = Annotated[float, BeforeValidator(_coerce_float), Field(ge=0, strict=True)]
UnitRatio = Annotated[float, BeforeValidator(_coerce_float), Field(ge=0, le=1, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]

#: The implementation-plan §10.4 measurement list — exactly the fields the
#: coverage summary counts. Locked to the plan by
#: tests/metrics/test_episode_run_report.py.
MEASURE_FIELDS: Final[tuple[str, ...]] = (
    "total_source_minutes",
    "output_minutes",
    "ttfrp_minutes",
    "total_wall_clock_minutes",
    "universal_pass_wall_clock_minutes",
    "deep_review_wall_clock_minutes",
    "deep_review_source_minute_ratio",
    "vision_frames",
    "vision_tokens",
    "vision_cost_usd",
    "cache_hit_ratio",
    "human_review_minutes",
    "human_blocking_sessions",
    "manual_resolve_edit_minutes",
    "keep_remove_corrections",
    "broll_corrections",
    "subtitle_corrections",
    "effect_corrections",
    "missed_valuable_moments",
    "blocking_defects",
    "manual_fallback_capabilities",
    "domain_statuses",
    "interruption_counts",
    "publishability_publishable",
    "publishability_reasons",
    "taste_evidence_used",
    "kit_recipes_used",
    "kit_recipes_overridden",
    "kit_recipes_manual_corrected",
    "cli_required",
    "json_required",
    "direct_resolve_required",
)


# ----------------------------------------------------------- coverage


class CoverageGap(StrictModel):
    """One null measure field and why it is null."""

    field: str
    reason: str

    @model_validator(mode="after")
    def require_known_field(self) -> CoverageGap:
        if self.field not in MEASURE_FIELDS:
            raise PydanticCustomError(
                "unknown_measure_field", "unknown field {field}", {"field": self.field}
            )
        if not self.reason.strip():
            raise PydanticCustomError("reason_required", "gap reason must be non-empty")
        return self


class FieldCoverage(StrictModel):
    """filled/total over MEASURE_FIELDS plus one gap per null field."""

    filled: NonNegativeInt
    total: NonNegativeInt
    gaps: Annotated[tuple[CoverageGap, ...], BeforeValidator(_to_tuple)] = ()

    @model_validator(mode="after")
    def require_accounting(self) -> FieldCoverage:
        if self.total != len(MEASURE_FIELDS):
            raise PydanticCustomError("total_mismatch", "total must equal the §10.4 field count")
        names = [gap.field for gap in self.gaps]
        if len(names) != len(set(names)):
            raise PydanticCustomError("duplicate_gap", "gap fields must be unique")
        if self.filled + len(self.gaps) != self.total:
            raise PydanticCustomError("coverage_accounting", "filled + gaps must equal total")
        return self


def _coverage_from_values(
    values: Mapping[str, object], reason_for: Mapping[str, str]
) -> FieldCoverage:
    gaps = tuple(
        CoverageGap(field=name, reason=reason_for.get(name, "operator measurement not provided"))
        for name in MEASURE_FIELDS
        if values.get(name) is None
    )
    return FieldCoverage(
        filled=len(MEASURE_FIELDS) - len(gaps), total=len(MEASURE_FIELDS), gaps=gaps
    )


# ------------------------------------------------------- report model


class EpisodeRunReportV1(StrictModel):
    """One episode's §10.4 measurement record (explicit nulls allowed)."""

    schema_version: Literal["episode-run-report-v1"]
    episode_class: EpisodeClassName
    episode_id: str | None = None
    class_attributed_failure: bool | None = None
    evidence_refs: StrSeq = ()
    field_coverage: FieldCoverage

    total_source_minutes: NonNegativeFloat | None = None
    output_minutes: NonNegativeFloat | None = None
    ttfrp_minutes: NonNegativeFloat | None = None
    total_wall_clock_minutes: NonNegativeFloat | None = None
    universal_pass_wall_clock_minutes: NonNegativeFloat | None = None
    deep_review_wall_clock_minutes: NonNegativeFloat | None = None
    deep_review_source_minute_ratio: NonNegativeFloat | None = None
    vision_frames: NonNegativeInt | None = None
    vision_tokens: NonNegativeInt | None = None
    vision_cost_usd: NonNegativeFloat | None = None
    cache_hit_ratio: UnitRatio | None = None
    human_review_minutes: NonNegativeFloat | None = None
    human_blocking_sessions: NonNegativeInt | None = None
    manual_resolve_edit_minutes: NonNegativeFloat | None = None
    keep_remove_corrections: NonNegativeInt | None = None
    broll_corrections: NonNegativeInt | None = None
    subtitle_corrections: NonNegativeInt | None = None
    effect_corrections: NonNegativeInt | None = None
    missed_valuable_moments: NonNegativeInt | None = None
    blocking_defects: NonNegativeInt | None = None
    manual_fallback_capabilities: StrSeq | None = None
    domain_statuses: dict[str, str] | None = None
    interruption_counts: dict[InterruptionLevelName, NonNegativeInt] | None = None
    publishability_publishable: bool | None = None
    publishability_reasons: StrSeq | None = None
    taste_evidence_used: StrSeq | None = None
    kit_recipes_used: StrSeq | None = None
    kit_recipes_overridden: StrSeq | None = None
    kit_recipes_manual_corrected: StrSeq | None = None
    cli_required: bool | None = None
    json_required: bool | None = None
    direct_resolve_required: bool | None = None

    @model_validator(mode="after")
    def require_invariants(self) -> EpisodeRunReportV1:
        if self.domain_statuses is not None:
            if set(self.domain_statuses) != set(QUALITY_DOMAINS):
                raise PydanticCustomError(
                    "domain_set_mismatch",
                    "domain_statuses must carry exactly the seven quality domains",
                )
            if not set(self.domain_statuses.values()) <= _VALID_STATUSES:
                raise PydanticCustomError(
                    "status_vocabulary", "domain statuses must use the T40 vocabulary"
                )
        if self.interruption_counts is not None and set(self.interruption_counts) != set(
            INTERRUPTION_LEVELS
        ):
            raise PydanticCustomError(
                "levels_incomplete",
                "interruption_counts must cover all three policy classes",
            )
        null_fields = sorted(name for name in MEASURE_FIELDS if getattr(self, name) is None)
        gap_names = sorted(gap.field for gap in self.field_coverage.gaps)
        expected_filled = len(MEASURE_FIELDS) - len(null_fields)
        if self.field_coverage.filled != expected_filled or gap_names != null_fields:
            raise PydanticCustomError(
                "coverage_mismatch",
                "field_coverage must exactly mirror the null measure fields",
            )
        return self


class OperatorEpisodeMeasurements(StrictModel):
    """Operator-fillable overlay: every §10.4 field optional, null = unfilled."""

    episode_id: str | None = None
    class_attributed_failure: bool | None = None
    total_source_minutes: NonNegativeFloat | None = None
    output_minutes: NonNegativeFloat | None = None
    ttfrp_minutes: NonNegativeFloat | None = None
    total_wall_clock_minutes: NonNegativeFloat | None = None
    universal_pass_wall_clock_minutes: NonNegativeFloat | None = None
    deep_review_wall_clock_minutes: NonNegativeFloat | None = None
    deep_review_source_minute_ratio: NonNegativeFloat | None = None
    vision_frames: NonNegativeInt | None = None
    vision_tokens: NonNegativeInt | None = None
    vision_cost_usd: NonNegativeFloat | None = None
    cache_hit_ratio: UnitRatio | None = None
    human_review_minutes: NonNegativeFloat | None = None
    human_blocking_sessions: NonNegativeInt | None = None
    manual_resolve_edit_minutes: NonNegativeFloat | None = None
    keep_remove_corrections: NonNegativeInt | None = None
    broll_corrections: NonNegativeInt | None = None
    subtitle_corrections: NonNegativeInt | None = None
    effect_corrections: NonNegativeInt | None = None
    missed_valuable_moments: NonNegativeInt | None = None
    blocking_defects: NonNegativeInt | None = None
    manual_fallback_capabilities: StrSeq | None = None
    domain_statuses: dict[str, str] | None = None
    interruption_counts: dict[InterruptionLevelName, NonNegativeInt] | None = None
    publishability_publishable: bool | None = None
    publishability_reasons: StrSeq | None = None
    taste_evidence_used: StrSeq | None = None
    kit_recipes_used: StrSeq | None = None
    kit_recipes_overridden: StrSeq | None = None
    kit_recipes_manual_corrected: StrSeq | None = None
    cli_required: bool | None = None
    json_required: bool | None = None
    direct_resolve_required: bool | None = None


def apply_measurements(
    report: EpisodeRunReportV1, measurements: OperatorEpisodeMeasurements
) -> EpisodeRunReportV1:
    """Overlay non-null operator measurements and recompute coverage."""
    merged = report.model_dump(mode="json")
    merged.update(measurements.model_dump(mode="json", exclude_none=True))
    merged["field_coverage"] = _coverage_from_values(merged, {}).model_dump(mode="json")
    return EpisodeRunReportV1.model_validate(merged)


# ---------------------------------------------------------- discovery

_MAX_SNIFF_BYTES: Final = 4 * 1024 * 1024

_EPISODE0_FIELDS: Final = frozenset(
    {
        "ttfrp_minutes",
        "total_wall_clock_minutes",
        "manual_resolve_edit_minutes",
        "keep_remove_corrections",
        "missed_valuable_moments",
        "total_source_minutes",
        "publishability_publishable",
        "publishability_reasons",
    }
)
_QUALITY_FIELDS: Final = frozenset(
    {"domain_statuses", "manual_fallback_capabilities", "blocking_defects"}
)
_INTERRUPTION_FIELDS: Final = frozenset({"interruption_counts"})

_VALID_STATUSES: Final = frozenset(get_args(QualityDomainStatus))
_UPLOAD_STATUSES: Final = frozenset({"started", "completed", "failed"})


class _Discovered:
    __slots__ = ("editorial_qc", "episode0_runs", "interruption", "invalid", "ledger", "quality")

    def __init__(self) -> None:
        self.episode0_runs: list[tuple[str, Episode0ReportV1]] = []
        self.quality: tuple[str, QualityDomainReportV1] | None = None
        self.editorial_qc: str | None = None
        self.interruption: tuple[str, dict[str, int]] | None = None
        self.ledger: str | None = None
        self.invalid: set[str] = set()

    def latest_episode0(self) -> tuple[str, Episode0ReportV1] | None:
        # Dated reruns outrank the frozen "baseline" run (T5 run-id contract).
        dated = [run for run in self.episode0_runs if run[0].rsplit("/", 2)[-2] != "baseline"]
        pool = dated or self.episode0_runs
        return max(pool, key=lambda run: run[0]) if pool else None


def _as_interruption_summary(payload: dict[str, object]) -> dict[str, int] | None:
    if set(payload) != {"counts", "blocking_count", "notices"}:
        return None
    counts = payload["counts"]
    if not isinstance(counts, dict) or set(counts) != set(INTERRUPTION_LEVELS):
        return None
    typed: dict[str, int] = {}
    for level, count in counts.items():
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return None
        typed[level] = count
    return typed


def _ledger_line_shape(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    key = record.get("idempotency_key")
    status = record.get("status")
    stamp = record.get("timestamp")
    return (
        isinstance(key, str)
        and bool(key)
        and isinstance(status, str)
        and status in _UPLOAD_STATUSES
        and isinstance(stamp, str)
        and bool(stamp)
    )


def _looks_like_upload_ledger(path: Path) -> bool:
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    lines = [line for line in raw.splitlines() if line]
    if not lines:
        return False
    try:
        parsed = [json.loads(line) for line in lines]
    except json.JSONDecodeError:
        return False
    return all(_ledger_line_shape(record) for record in parsed)


def _discover(root: Path) -> _Discovered:  # noqa: C901, PLR0912 (flat schema sniff)
    found = _Discovered()
    candidates = sorted({*root.rglob("*.json"), *root.rglob("*.jsonl")})
    for path in candidates:
        try:
            if path.stat().st_size > _MAX_SNIFF_BYTES:
                continue
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        if path.suffix == ".jsonl":
            if found.ledger is None and _looks_like_upload_ledger(path):
                found.ledger = rel
            continue
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        schema = payload.get("schema_version")
        if schema == "episode-run-report-v1":
            continue  # our own output is never an input
        if schema == "episode0-report-v1":
            try:
                parsed = Episode0ReportV1.model_validate(payload)
            except ValidationError:
                found.invalid.add("episode0-report-v1")
                continue
            found.episode0_runs.append((rel, parsed))
        elif schema == "quality-domain-report-v1":
            try:
                parsed = QualityDomainReportV1.model_validate(payload)
            except ValidationError:
                found.invalid.add("quality-domain-report-v1")
                continue
            if found.quality is None or rel > found.quality[0]:
                found.quality = (rel, parsed)
        elif schema == "editorial-qc-report-v1":
            if found.editorial_qc is None or rel > found.editorial_qc:
                found.editorial_qc = rel
        else:
            summary = _as_interruption_summary(payload)
            if summary is not None and (found.interruption is None or rel > found.interruption[0]):
                found.interruption = (rel, summary)
    return found


def _gap_reasons(found: _Discovered) -> dict[str, str]:
    reasons: dict[str, str] = {}
    latest = found.latest_episode0()
    if latest is None:
        if "episode0-report-v1" in found.invalid:
            reason = "episode0-report-v1 present but invalid"
        else:
            reason = "episode0-report-v1 run report absent"
        for field in _EPISODE0_FIELDS:
            reasons[field] = reason
    else:
        reasons["total_source_minutes"] = "episode0 manifest duration not probed"
    for field in _QUALITY_FIELDS:
        if found.quality is None:
            reasons[field] = (
                "quality-domain-report-v1 present but invalid"
                if "quality-domain-report-v1" in found.invalid
                else "quality-domain-report-v1 absent"
            )
    for field in _INTERRUPTION_FIELDS:
        if found.interruption is None:
            reasons[field] = "interruption summary absent"
    return reasons


def collect_episode_run(
    episode_root: Path,
    *,
    episode_class: EpisodeClassName,
    operator_measurements: OperatorEpisodeMeasurements | None = None,
) -> EpisodeRunReportV1:
    """Assemble one episode's §10.4 report from its run artifacts.

    Missing inputs leave explicit nulls with named coverage gaps; nothing is
    fabricated and no field is inferred beyond the documented derivations.
    """
    if not episode_root.is_dir():
        raise EpisodeRunReportError(
            "episode-root-missing", f"episode root is not a directory: {episode_root}"
        )
    found = _discover(episode_root)
    values: dict[str, object] = {}
    for name in MEASURE_FIELDS:
        values[name] = None
    evidence: list[str] = []
    latest = found.latest_episode0()
    if latest is not None:
        rel, episode0 = latest
        evidence.append(rel)
        log = episode0.log
        values["ttfrp_minutes"] = log.ttfrp_minutes
        values["total_wall_clock_minutes"] = log.wall_clock_minutes
        values["manual_resolve_edit_minutes"] = log.manual_resolve_minutes
        values["keep_remove_corrections"] = len(log.wrong_keep_remove)
        values["missed_valuable_moments"] = len(log.missed_moments)
        if episode0.manifest.duration_probed and episode0.manifest.duration_seconds is not None:
            values["total_source_minutes"] = episode0.manifest.duration_seconds / 60
        values["publishability_publishable"] = log.publishability.publishable
        values["publishability_reasons"] = tuple(
            part
            for part in (
                log.publishability.comment,
                log.publishability.best_ts,
                log.publishability.worst_ts,
            )
            if part
        )
    if found.quality is not None:
        rel, quality = found.quality
        evidence.append(rel)
        values["domain_statuses"] = {entry.domain: entry.status for entry in quality.domains}
        values["manual_fallback_capabilities"] = tuple(
            entry.domain for entry in quality.domains if entry.status == "manual_fallback_required"
        )
        values["blocking_defects"] = sum(
            1 for entry in quality.domains if entry.status == "blocked"
        )
    if found.interruption is not None:
        rel, counts = found.interruption
        evidence.append(rel)
        values["interruption_counts"] = counts
    evidence.extend(rel for rel in (found.editorial_qc, found.ledger) if rel is not None)
    payload = {
        "schema_version": EPISODE_RUN_REPORT_SCHEMA,
        "episode_class": episode_class,
        "evidence_refs": tuple(sorted(evidence)),
        "field_coverage": _coverage_from_values(values, _gap_reasons(found)).model_dump(
            mode="json"
        ),
        **values,
    }
    report: EpisodeRunReportV1 = EpisodeRunReportV1.model_validate(payload)
    if operator_measurements is not None:
        report = apply_measurements(report, operator_measurements)
    return report


def write_report(report: EpisodeRunReportV1, path: Path) -> Path:
    """Write canonical report bytes (stable across re-runs)."""
    atomic_write(path, canonical_model_bytes(report))
    return path


# ------------------------------------------------------- aggregation


class AutomationRateEstimateV1(StrictModel):
    """Kit-recipe-based automation estimate (80% target context only)."""

    automated_actions: NonNegativeInt
    manual_actions: NonNegativeInt
    rate: UnitRatio | None = None
    target_rate: TargetRate = DEFAULT_AUTOMATION_TARGET
    meets_target: bool | None = None

    @model_validator(mode="after")
    def require_rate_consistency(self) -> AutomationRateEstimateV1:
        total = self.automated_actions + self.manual_actions
        if total == 0:
            if self.rate is not None or self.meets_target is not None:
                raise PydanticCustomError(
                    "rate_without_actions", "zero actions cannot carry a rate"
                )
        else:
            expected = self.automated_actions / total
            if self.rate is None or self.rate != expected:
                raise PydanticCustomError(
                    "rate_mismatch", "rate must equal automated / (automated + manual)"
                )
            if self.meets_target is not (self.rate >= self.target_rate):
                raise PydanticCustomError(
                    "meets_target_mismatch", "meets_target must equal rate >= target_rate"
                )
        return self


class Phase6EpisodeRowV1(StrictModel):
    """One episode's rolled-up row in the Phase-6 summary."""

    episode_class: EpisodeClassName
    episode_id: str | None = None
    field_coverage: FieldCoverage
    class_attributed_failure: bool | None = None
    kit_recipes_used_count: NonNegativeInt | None = None
    kit_recipes_overridden_count: NonNegativeInt | None = None
    kit_recipes_manual_corrected_count: NonNegativeInt | None = None
    manual_fallback_capability_count: NonNegativeInt | None = None
    cli_required: bool | None = None
    json_required: bool | None = None
    direct_resolve_required: bool | None = None


class Phase6CoverageV1(StrictModel):
    """Summed field coverage across the aggregated reports."""

    reports: NonNegativeInt
    complete_reports: NonNegativeInt
    filled: NonNegativeInt
    total: NonNegativeInt


class Phase6SummaryV1(StrictModel):
    """Phase-6 exit-criteria summary over the three episode reports."""

    schema_version: Literal["phase6-summary-v1"]
    rows: Annotated[tuple[Phase6EpisodeRowV1, ...], BeforeValidator(_to_tuple)]
    non_talking_head_failure_zero: bool
    non_talking_head_failure_asserted: bool
    automation_rate_estimate: AutomationRateEstimateV1
    coverage: Phase6CoverageV1

    @model_validator(mode="after")
    def require_coverage_accounting(self) -> Phase6SummaryV1:
        total = len(self.rows) * len(MEASURE_FIELDS)
        filled = sum(row.field_coverage.filled for row in self.rows)
        complete = sum(
            1 for row in self.rows if row.field_coverage.filled == row.field_coverage.total
        )
        actual = (
            self.coverage.reports,
            self.coverage.total,
            self.coverage.filled,
            self.coverage.complete_reports,
        )
        if actual != (len(self.rows), total, filled, complete):
            raise PydanticCustomError(
                "coverage_accounting", "coverage must mirror the rows exactly"
            )
        return self


def _row_for(report: EpisodeRunReportV1) -> Phase6EpisodeRowV1:
    def count(field: str) -> int | None:
        value = getattr(report, field)
        return None if value is None else len(value)

    return Phase6EpisodeRowV1(
        episode_class=report.episode_class,
        episode_id=report.episode_id,
        field_coverage=report.field_coverage,
        class_attributed_failure=report.class_attributed_failure,
        kit_recipes_used_count=count("kit_recipes_used"),
        kit_recipes_overridden_count=count("kit_recipes_overridden"),
        kit_recipes_manual_corrected_count=count("kit_recipes_manual_corrected"),
        manual_fallback_capability_count=count("manual_fallback_capabilities"),
        cli_required=report.cli_required,
        json_required=report.json_required,
        direct_resolve_required=report.direct_resolve_required,
    )


def aggregate_three_episodes(
    reports: Sequence[EpisodeRunReportV1],
) -> Phase6SummaryV1:
    """Fold the A/B/C reports into the Phase-6 exit-criteria summary."""
    if not reports:
        raise EpisodeRunReportError("no-reports", "aggregation needs at least one report")
    rows = tuple(_row_for(report) for report in reports)
    non_talking_head = [row for row in rows if row.episode_class != "a_talking_broll"]
    failure_zero = not any(row.class_attributed_failure is True for row in non_talking_head)
    asserted = bool(non_talking_head) and all(
        row.class_attributed_failure is not None for row in non_talking_head
    )
    automated = sum(row.kit_recipes_used_count or 0 for row in rows)
    manual = sum(
        (row.kit_recipes_overridden_count or 0)
        + (row.kit_recipes_manual_corrected_count or 0)
        + (row.manual_fallback_capability_count or 0)
        for row in rows
    )
    actions = automated + manual
    estimate = AutomationRateEstimateV1(
        automated_actions=automated,
        manual_actions=manual,
        rate=automated / actions if actions else None,
        meets_target=(automated / actions >= DEFAULT_AUTOMATION_TARGET) if actions else None,
    )
    coverage = Phase6CoverageV1(
        reports=len(rows),
        complete_reports=sum(
            1 for row in rows if row.field_coverage.filled == row.field_coverage.total
        ),
        filled=sum(row.field_coverage.filled for row in rows),
        total=len(rows) * len(MEASURE_FIELDS),
    )
    return Phase6SummaryV1(
        schema_version=PHASE6_SUMMARY_SCHEMA,
        rows=rows,
        non_talking_head_failure_zero=failure_zero,
        non_talking_head_failure_asserted=asserted,
        automation_rate_estimate=estimate,
        coverage=coverage,
    )


# ------------------------------------------------- footage decision

_SYNTHETIC_FALLBACK_PROPOSAL: Final = (
    "Operator footage not provided: propose synthetic screen-capture footage "
    "as the Phase-6 fallback for operator review (plan task 52) — recorded as "
    "a proposal; no indefinite wait for operator-provided sources."
)
_REAL01_PROVISION_PROPOSAL: Final = (
    "real-01 episode.json not found: provision real-01 (the plan's Episode B "
    "source) before the Phase-6 episode runs."
)


class FootageSourceCheckV1(StrictModel):
    """One episode class's pre-start footage decision."""

    episode_class: EpisodeClassName
    status: Literal["confirmed", "fallback_proposed"]
    sources: StrSeq = ()
    note: str
    fallback_proposal: str | None = None

    @model_validator(mode="after")
    def require_proposal_contract(self) -> FootageSourceCheckV1:
        if self.status == "fallback_proposed":
            if not (self.fallback_proposal and self.fallback_proposal.strip()):
                raise PydanticCustomError(
                    "proposal_required", "fallback_proposed requires a proposal"
                )
        elif self.fallback_proposal is not None:
            raise PydanticCustomError(
                "proposal_only_on_fallback", "confirmed sources carry no proposal"
            )
        return self


class FootageDecisionV1(StrictModel):
    """Pre-start decision record for the three Phase-6 footage sources."""

    schema_version: Literal["footage-decision-v1"]
    checks: Annotated[tuple[FootageSourceCheckV1, ...], BeforeValidator(_to_tuple)]
    wait_policy: Literal["no_indefinite_wait"] = "no_indefinite_wait"

    @model_validator(mode="after")
    def require_all_classes(self) -> FootageDecisionV1:
        classes = tuple(check.episode_class for check in self.checks)
        if classes != EPISODE_CLASSES:
            raise PydanticCustomError(
                "class_set_mismatch",
                "checks must cover exactly the three episode classes in order",
            )
        return self


class FootageOperatorInputs(StrictModel):
    """Operator-declared source paths for the pre-start check."""

    real01_episode_json: str = "private/reference-episodes/real-01/episode.json"
    episode_a_sources: StrSeq = ()
    episode_c_sources: StrSeq = ()


def _source_check(
    episode_class: EpisodeClassName,
    provided: Sequence[str],
    *,
    synthetic: bool,
) -> FootageSourceCheckV1:
    existing = tuple(source for source in provided if Path(source).is_file())
    if existing:
        return FootageSourceCheckV1(
            episode_class=episode_class,
            status="confirmed",
            sources=existing,
            note=f"{len(existing)} of {len(provided)} provided source(s) present on disk",
        )
    missing_note = f"{len(provided)} provided source path(s), none present on disk"
    return FootageSourceCheckV1(
        episode_class=episode_class,
        status="fallback_proposed",
        sources=(),
        note=missing_note,
        fallback_proposal=(
            _SYNTHETIC_FALLBACK_PROPOSAL if synthetic else _REAL01_PROVISION_PROPOSAL
        ),
    )


def check_footage_sources(operator_inputs: FootageOperatorInputs) -> FootageDecisionV1:
    """Decide the three footage sources before Phase 6 starts.

    Episode B is served by real-01; absent A/C footage yields an explicit
    fallback PROPOSAL (synthetic screen capture) instead of waiting.
    """
    real01_json = Path(operator_inputs.real01_episode_json)
    b_check = FootageSourceCheckV1(
        episode_class="b_visual_first",
        status="confirmed" if real01_json.is_file() else "fallback_proposed",
        sources=(str(real01_json),) if real01_json.is_file() else (),
        note=f"real-01 episode.json at {real01_json.as_posix()}",
        fallback_proposal=None if real01_json.is_file() else _REAL01_PROVISION_PROPOSAL,
    )
    return FootageDecisionV1(
        schema_version=FOOTAGE_DECISION_SCHEMA,
        checks=(
            _source_check("a_talking_broll", operator_inputs.episode_a_sources, synthetic=True),
            b_check,
            _source_check("c_mixed", operator_inputs.episode_c_sources, synthetic=True),
        ),
    )


__all__ = [
    "EPISODE_CLASSES",
    "MEASURE_FIELDS",
    "AutomationRateEstimateV1",
    "CoverageGap",
    "EpisodeClassName",
    "EpisodeRunReportError",
    "EpisodeRunReportV1",
    "FieldCoverage",
    "FootageDecisionV1",
    "FootageOperatorInputs",
    "FootageSourceCheckV1",
    "OperatorEpisodeMeasurements",
    "Phase6CoverageV1",
    "Phase6EpisodeRowV1",
    "Phase6SummaryV1",
    "ValidationError",
    "aggregate_three_episodes",
    "apply_measurements",
    "check_footage_sources",
    "collect_episode_run",
    "write_report",
]
