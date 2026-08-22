# allow: SIZE_OK — task 30 pins the measurement contract + Gate V43-2
# machine checklist to this metrics module (report + gate + compare).
# Single responsibility: "the rerun's measurement artifact + its machine
# checklist". T29/T31/T38 single-module precedent.
"""Episode-0 editorial-v2 rerun measurement + Gate V43-2 checklist (task 30).

Two cohesive contracts, pinned by task 30 to this module:

(a) ``Episode0RerunReportV1`` — the measurement report written to
    ``runs/<run-id>/report.json`` by the ``episode0 run --phase editorial-v2``
    harness. Operator-measurable fields mirror ``Episode0BaselineLogV1``
    field names but stay ``null`` until the operator fills them (never
    fabricated); machine-measured editorial fields (three-pass refs, taste
    citations, miss/wrong-decision inputs, hallucination count) are filled
    by the harness.

(b) ``check_gate`` — the Gate V43-2 machine checklist (PRD 1587-1604,
    task-30 subset): story plan generated; non-speech candidates present;
    domain-scoped taste present-or-explicitly-absent; no invented
    spans/IDs; review-event corrections path exercised. The report's claims
    are cross-checked against independently recomputed ``GateEvidenceV1``.

``compare_rerun`` extends T5's delta with the editorial additions (kept
non-speech count, evidence-coverage rate, hallucination count, taste
citations); pending operator fields yield null deltas.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.editorial_v2.taste_retrieval import TasteCitation  # noqa: TC001 (pydantic field)
from services.metrics.episode0_baseline import (  # noqa: TC001 (pydantic fields)
    Episode0ReportV1,
    Publishability,
    TimestampNote,
)

RERUN_REPORT_SCHEMA: Literal["episode0-rerun-report-v1"] = "episode0-rerun-report-v1"  # type: ignore[assignment] — Literal for schema field, narrowed by validator
_MIN_COMMITTED_EVENTS = 2


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


# ------------------------------------------------------------ report models


class ArtifactFileRef(StrictModel):
    """One pipeline artifact file written under the run directory."""

    path: str
    sha256: Sha256


class RerunInputsV1(StrictModel):
    reference_episode_id: Identifier
    brief: ArtifactFileRef
    media_intelligence: ArtifactFileRef
    taste_profile: ArtifactFileRef | None = None
    source_media: ArtifactFileRef | None = None
    frame_rate: str


class RerunThreePassV1(StrictModel):
    story_plan: ArtifactFileRef
    moment_selection: ArtifactFileRef
    creative_edit: ArtifactFileRef
    creative_plan: ArtifactFileRef
    timeline_ir: ArtifactFileRef


class RerunValidationV1(StrictModel):
    candidates_checked: int = Field(ge=1, strict=True)
    refs_verified: int = Field(ge=0, strict=True)
    hallucination_count: int = Field(ge=0, strict=True)
    hallucinated_refs: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = ()

    @model_validator(mode="after")
    def require_count_matches_refs(self) -> RerunValidationV1:
        if self.hallucination_count != len(self.hallucinated_refs):
            raise ValueError("hallucination_count must equal len(hallucinated_refs)")
        return self


class RerunCommitV1(StrictModel):
    version: int = Field(ge=2, strict=True)
    event_id: Sha256
    proposal_sha256: Sha256


class RerunReviewCorrectionV1(StrictModel):
    exercised: bool
    synthetic: bool = True
    version_from: int | None = None
    version_to: int | None = None
    event_id: Sha256 | None = None
    corrected_candidate_id: str | None = None
    note: str = ""


class RerunTasteV1(StrictModel):
    profile_supplied: bool
    explicitly_absent: bool
    citation_count: int = Field(ge=0, strict=True)
    citations: Annotated[tuple[TasteCitation, ...], BeforeValidator(_to_tuple)] = ()


class RerunEditorialMetricsV1(StrictModel):
    story_plan_block_count: int = Field(ge=1, strict=True)
    selection_candidate_count: int = Field(ge=1, strict=True)
    non_speech_candidate_count: int = Field(ge=0, strict=True)
    kept_non_speech_count: int = Field(ge=0, strict=True)
    kept_speech_count: int = Field(ge=0, strict=True)
    removed_count: int = Field(ge=0, strict=True)
    evidence_coverage_rate: float = Field(ge=0.0, le=1.0, strict=True)


class RerunPreviewV1(StrictModel):
    preview_path: str | None = None
    preview_sha256: Sha256 | None = None
    total_record_frames: int | None = Field(default=None, ge=1, strict=True)
    skipped_reason: str | None = None


class RerunOperatorFieldsV1(StrictModel):
    """Episode0BaselineLogV1-compatible names; operator-fillable, never fabricated."""

    active_human_time_minutes: float | None = None
    ttfrp_minutes: float | None = None
    wall_clock_minutes: float | None = None
    manual_resolve_minutes: float | None = None
    wrong_keep_remove: tuple[TimestampNote, ...] | None = None
    missed_moments: tuple[TimestampNote, ...] | None = None
    finishing_deficits: tuple[TimestampNote, ...] | None = None
    interruption_points: tuple[TimestampNote, ...] | None = None
    publishability: Publishability | None = None


class Episode0RerunReportV1(StrictModel):
    schema_version: Literal["episode0-rerun-report-v1"]
    run_id: str
    phase: Literal["editorial-v2"]
    episode_id: Identifier
    inputs: RerunInputsV1
    three_pass: RerunThreePassV1
    validation: RerunValidationV1
    commit: RerunCommitV1
    review_correction: RerunReviewCorrectionV1
    taste: RerunTasteV1
    editorial: RerunEditorialMetricsV1
    preview: RerunPreviewV1
    operator: RerunOperatorFieldsV1 = RerunOperatorFieldsV1()
    gate_check_path: str = "gate-check.json"


# ------------------------------------------------------------ gate checklist


class GateCriterionV1(StrictModel):
    requirement: str
    status: Literal["pass", "fail"]
    detail: str


class GateCheckV1(StrictModel):
    schema_version: Literal["episode0-gate-check-v1"]
    gate: Literal["V43-2"]
    run_id: str
    passed: bool
    criteria: Annotated[tuple[GateCriterionV1, ...], BeforeValidator(_to_tuple)] = Field(
        min_length=1
    )


class GateEvidenceV1(StrictModel):
    """Independently recomputed evidence cross-checking the report's claims."""

    story_plan_block_count: int = Field(ge=0, strict=True)
    selection_candidate_count: int = Field(ge=1, strict=True)
    non_speech_candidate_count: int = Field(ge=0, strict=True)
    review_committed_event_count: int = Field(ge=0, strict=True)
    hallucinated_refs: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = ()


def _criterion(requirement: str, *, ok: bool, detail: str) -> GateCriterionV1:
    return GateCriterionV1(requirement=requirement, status="pass" if ok else "fail", detail=detail)


def check_gate(report: Episode0RerunReportV1, evidence: GateEvidenceV1) -> GateCheckV1:
    """Verify the Gate V43-2 machine checklist; never author a pass without proof."""

    taste_ok = report.taste.citation_count >= 1 or report.taste.explicitly_absent
    invented = sorted({*report.validation.hallucinated_refs, *evidence.hallucinated_refs})
    criteria = (
        _criterion(
            "story-plan-generated",
            ok=(
                report.editorial.story_plan_block_count >= 1
                and evidence.story_plan_block_count >= 1
            ),
            detail=(
                f"story plan blocks: report={report.editorial.story_plan_block_count} "
                f"evidence={evidence.story_plan_block_count}"
            ),
        ),
        _criterion(
            "non-speech-candidates-present",
            ok=evidence.non_speech_candidate_count >= 1,
            detail=f"non-speech candidates in selection: {evidence.non_speech_candidate_count}",
        ),
        _criterion(
            "domain-scoped-taste",
            ok=taste_ok,
            detail=(
                f"taste citations={report.taste.citation_count} "
                f"explicitly_absent={report.taste.explicitly_absent}"
            ),
        ),
        _criterion(
            "no-invented-spans-ids",
            ok=not invented,
            detail=(
                f"hallucination count={report.validation.hallucination_count}; "
                f"invented refs={invented or 'none'}"
            ),
        ),
        _criterion(
            "review-event-corrections",
            ok=(
                report.review_correction.exercised
                and evidence.review_committed_event_count >= _MIN_COMMITTED_EVENTS
            ),
            detail=(
                f"corrections exercised={report.review_correction.exercised}, committed "
                f"moment-selection events={evidence.review_committed_event_count}"
            ),
        ),
    )
    return GateCheckV1(
        schema_version="episode0-gate-check-v1",
        gate="V43-2",
        run_id=report.run_id,
        passed=all(c.status == "pass" for c in criteria),
        criteria=criteria,
    )


# ------------------------------------------------------------ compare delta


def _tri(a_val: float, b_val: float | None) -> dict[str, float | None]:
    return {"a": a_val, "b": b_val, "delta": None if b_val is None else b_val - a_val}


def _count_tri(a_count: int, b_count: int | None) -> dict[str, int | None]:
    return {
        "a": a_count,
        "b": b_count,
        "delta": None if b_count is None else b_count - a_count,
    }


def compare_rerun(baseline: Episode0ReportV1, rerun: Episode0RerunReportV1) -> dict[str, object]:
    """Baseline (v1 report) vs editorial-v2 rerun; pending operator fields → null deltas."""

    op = rerun.operator
    pending = any(
        value is None
        for value in (
            op.active_human_time_minutes,
            op.ttfrp_minutes,
            op.wall_clock_minutes,
            op.manual_resolve_minutes,
        )
    )
    baseline_media = rerun.inputs.source_media
    return {
        "a_run_id": baseline.run_id,
        "b_run_id": rerun.run_id,
        "phase": rerun.phase,
        "active_human_time_minutes": _tri(
            float(baseline.log.active_human_time_minutes), op.active_human_time_minutes
        ),
        "ttfrp_minutes": _tri(float(baseline.log.ttfrp_minutes), op.ttfrp_minutes),
        "wall_clock_minutes": _tri(float(baseline.log.wall_clock_minutes), op.wall_clock_minutes),
        "manual_resolve_minutes": _tri(
            float(baseline.log.manual_resolve_minutes), op.manual_resolve_minutes
        ),
        "wrong_keep_remove_count": _count_tri(
            len(baseline.log.wrong_keep_remove),
            None if op.wrong_keep_remove is None else len(op.wrong_keep_remove),
        ),
        "missed_moments_count": _count_tri(
            len(baseline.log.missed_moments),
            None if op.missed_moments is None else len(op.missed_moments),
        ),
        "finishing_deficits_count": _count_tri(
            len(baseline.log.finishing_deficits),
            None if op.finishing_deficits is None else len(op.finishing_deficits),
        ),
        "interruption_points_count": _count_tri(
            len(baseline.log.interruption_points),
            None if op.interruption_points is None else len(op.interruption_points),
        ),
        "publishability": {
            "a": baseline.log.publishability.model_dump(mode="json"),
            "b": None if op.publishability is None else op.publishability.model_dump(mode="json"),
        },
        "publishability_changed": (
            None
            if op.publishability is None
            else baseline.log.publishability.publishable != op.publishability.publishable
        ),
        "operator_measurement_pending": pending,
        "editorial": {
            "story_plan_block_count": rerun.editorial.story_plan_block_count,
            "kept_non_speech_count": rerun.editorial.kept_non_speech_count,
            "kept_speech_count": rerun.editorial.kept_speech_count,
            "removed_count": rerun.editorial.removed_count,
            "evidence_coverage_rate": rerun.editorial.evidence_coverage_rate,
            "hallucination_count": rerun.validation.hallucination_count,
            "taste_citation_count": rerun.taste.citation_count,
        },
        "hallucination_must_be_zero": {
            "count": rerun.validation.hallucination_count,
            "ok": rerun.validation.hallucination_count == 0,
        },
        "source_identity": {
            "baseline_manifest_sha256": baseline.manifest.sha256,
            "rerun_source_media_sha256": None if baseline_media is None else baseline_media.sha256,
            "same_source": (
                None
                if baseline_media is None
                else baseline_media.sha256 == baseline.manifest.sha256
            ),
        },
    }


__all__ = [
    "ArtifactFileRef",
    "Episode0RerunReportV1",
    "GateCheckV1",
    "GateCriterionV1",
    "GateEvidenceV1",
    "RerunCommitV1",
    "RerunEditorialMetricsV1",
    "RerunInputsV1",
    "RerunOperatorFieldsV1",
    "RerunPreviewV1",
    "RerunReviewCorrectionV1",
    "RerunTasteV1",
    "RerunThreePassV1",
    "RerunValidationV1",
    "check_gate",
    "compare_rerun",
]
