# allow: SIZE_OK — task 43 pins the full-build measurement contract + Gate
# V43-3 machine checklist to this metrics module (report sections + gate).
# Single responsibility: "the full-build run's measurement artifact + its
# machine checklist". T30 episode0_gate_v43_2 precedent.
"""Episode-0 FULL BUILD rerun measurement + Gate V43-3 checklist (task 43).

Two cohesive contracts, pinned by task 43 to this module:

(a) ``Episode0FullBuildReportV1`` — the build report written to
    ``runs/<run-id>/report.json`` by ``episode0 run --phase full-build``.
    It EMBEDS the task-30 editorial report verbatim (the editorial stages
    are the head of the full-build pipeline) and adds the build sections:
    presentation intents / finishing plans / kit selections / the compiled
    MCP execution plan / the T39 run report / the seven-domain quality
    report / editorial QC / the T60 stub-launch / the publishability
    scaffold (``publishable=null`` until the operator fills it — never
    fabricated) / the legacy rollback leg.

(b) ``check_gate`` — the Gate V43-3 machine checklist (PRD 1606-1623).
    PRD line → criterion mapping: 1610 → ``ir-v2-compiles-to-mcp-execution``;
    1611 → ``subtitles-usable-when-required``; 1612 →
    ``audio-plan-executed-checked``; 1613 → ``color-plan-or-justified-noop``
    (the T35 shared validator makes an unjustified no-op unrepresentable,
    so a present plan IS the proof); 1614 → ``broll-primary-coexist``; 1615
    (framing/graphics supported where justified) is carried by the domain
    statuses — ``seven-domains-explicit`` + ``no-blocked-domain-at-final``;
    1616 → ``seven-domains-explicit``; 1617 → ``recipes-resolved-via-kit``;
    1618 → ``recipe-provenance-recorded``; 1619 → ``no-blocked-domain-at-final``;
    1620 → ``readback-source-record-verified``; 1621 → ``render-qc-completes``;
    1622 → ``publishability-review-recorded``; 1623 →
    ``legacy-rollback-available``. There is deliberately no effect-count
    quota. The report's claims are cross-checked against independently
    recomputed ``FullBuildGateEvidenceV1`` (parsed from the run's
    artifacts, never from the report itself).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field

from services.contracts.primitives import Identifier, Sha256, StrictModel, to_tuple
from services.creative_plan.presentation_intents import (  # noqa: TC001 (pydantic runtime)
    PresentationIntentV2,
)
from services.final_review.publishability import Publishable  # noqa: TC001 (pydantic runtime)
from services.metrics.episode0_gate_v43_2 import (
    ArtifactFileRef,
    Episode0RerunReportV1,
    GateCriterionV1,
    RerunInputsV1,
    RerunOperatorFieldsV1,
)

FULL_BUILD_REPORT_SCHEMA: Literal["episode0-full-build-report-v1"] = "episode0-full-build-report-v1"

#: The PRD's seven quality domains (PRD 14.3) — statuses must cover exactly these.
SEVEN_DOMAINS: tuple[str, ...] = (
    "editorial_construction",
    "subtitle",
    "audio_finishing",
    "color_finishing",
    "framing_motion",
    "graphics_presentation",
    "delivery_qc",
)

_VALID_DOMAIN_STATUSES = frozenset(
    {"applied", "intentionally_not_needed", "manual_fallback_required", "blocked"}
)
_VALID_PUBLISHABLE = frozenset({"as_is", "after_small_corrections", "not_yet"})
_ROLLBACK_TRANSITIONS: tuple[str, str] = ("mcp->legacy_direct", "legacy_direct->mcp")


# ------------------------------------------------------------ report models


class BuildKitSelectionV1(StrictModel):
    """One recipe resolved through the versioned kit (never ad-hoc)."""

    intent: str = Field(min_length=1, strict=True)
    recipe_id: str = Field(min_length=1, strict=True)
    origin: str = Field(min_length=1, strict=True)
    license: str = Field(min_length=1, strict=True)
    fallback: str = Field(min_length=1, strict=True)
    rationale: str = Field(min_length=1, strict=True)
    resolved_params: dict[str, float] = Field(default_factory=dict)


class BuildPlansV1(StrictModel):
    """The committed pre-execution plans (T32/T33/T34/T35/T37/T38)."""

    presentation_intents: ArtifactFileRef
    presentation_intent_count: int = Field(ge=0, strict=True)
    density_within_limits: bool
    subtitle_plan: ArtifactFileRef
    subtitle_cue_count: int = Field(ge=0, strict=True)
    audio_plan: ArtifactFileRef
    color_plan: ArtifactFileRef
    kit_selections: Annotated[tuple[BuildKitSelectionV1, ...], BeforeValidator(to_tuple)]
    execution_plan: ArtifactFileRef
    plan_step_count: int = Field(ge=1, strict=True)
    plan_id: Sha256


class BuildExecutionV1(StrictModel):
    """The T39 runner outcome for the compiled plan."""

    executor: Literal["fake", "live"]
    outcome: Literal["completed", "failed"]
    step_count: int = Field(ge=1, strict=True)
    completed_step_count: int = Field(ge=0, strict=True)
    failed_step_count: int = Field(ge=0, strict=True)
    readback_matched_step_count: int = Field(ge=0, strict=True)
    run_report: ArtifactFileRef
    render_record: ArtifactFileRef


class BuildQualityV1(StrictModel):
    """Seven-domain quality report (T40) + editorial QC candidates (T41)."""

    domain_report: ArtifactFileRef
    domain_statuses: dict[str, str]
    blocked_domains: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = ()
    editorial_qc_report: ArtifactFileRef
    qc_candidate_count: int = Field(ge=0, strict=True)
    qc_critical_count: int = Field(ge=0, strict=True)
    qc_needs_review_count: int = Field(ge=0, strict=True)


class BuildPreviewV1(StrictModel):
    """The T60 presentation preview stub launch."""

    trace: ArtifactFileRef
    status: str = Field(min_length=1, strict=True)


class BuildPublishabilityV1(StrictModel):
    """T42 sparse-input scaffold; ``publishable`` stays null until filled."""

    scaffold: ArtifactFileRef
    publishable: Publishable | None = None
    filled: bool


class BuildRollbackV1(StrictModel):
    """The legacy-backend rollback leg executed inside the run."""

    checked: bool
    transitions: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = ()
    legacy_verified_backend: str | None = None


class Episode0FullBuildReportV1(StrictModel):
    schema_version: Literal["episode0-full-build-report-v1"]
    run_id: str
    phase: Literal["full-build"]
    episode_id: Identifier
    executor: Literal["fake", "live"]
    inputs: RerunInputsV1
    editorial: Episode0RerunReportV1
    plans: BuildPlansV1
    execution: BuildExecutionV1
    quality: BuildQualityV1
    preview: BuildPreviewV1
    publishability: BuildPublishabilityV1
    rollback: BuildRollbackV1
    operator: RerunOperatorFieldsV1 = RerunOperatorFieldsV1()
    gate_check_path: str = "gate-check-v43-3.json"


class PublishabilityScaffoldV1(StrictModel):
    """Operator-fillable T42 input record (``publishable=null`` pending)."""

    schema_version: Literal["publishability-review-input-v1"]
    episode_id: Identifier
    run_id: Identifier
    publishable: Publishable | None = None
    overall_comment: str | None = None
    best_ts_seconds: float | None = Field(default=None, ge=0.0, strict=True)
    worst_ts_seconds: float | None = Field(default=None, ge=0.0, strict=True)
    note: str = Field(min_length=1, strict=True)


class BuildRenderRecordV1(StrictModel):
    """Render/QC completion record; the fake executor fabricates no media."""

    schema_version: Literal["synthetic-render-record-v1"]
    executor: Literal["fake", "live"]
    episode_id: Identifier
    run_outcome: Literal["completed", "failed"]
    completed_steps: int = Field(ge=0, strict=True)
    note: str = Field(min_length=1, strict=True)


class PresentationIntentsArtifactV1(StrictModel):
    """The run's authored presentation-intent set + its density verdict."""

    schema_version: Literal["presentation-intents-v1"]
    episode_id: Identifier
    intents: Annotated[tuple[PresentationIntentV2, ...], BeforeValidator(to_tuple)] = ()
    timeline_duration_seconds: float = Field(gt=0.0, strict=True)
    density_within_limits: bool


class KitSelectionsArtifactV1(StrictModel):
    """The run's active recipes resolved through the versioned kit."""

    schema_version: Literal["kit-selections-v1"]
    episode_id: Identifier
    selections: Annotated[tuple[BuildKitSelectionV1, ...], BeforeValidator(to_tuple)]


# ------------------------------------------------------------ gate checklist


class FullBuildGateCheckV1(StrictModel):
    schema_version: Literal["episode0-gate-check-v1"]
    gate: Literal["V43-3"]
    run_id: str
    passed: bool
    criteria: Annotated[tuple[GateCriterionV1, ...], BeforeValidator(to_tuple)] = Field(
        min_length=1
    )


class FullBuildGateEvidenceV1(StrictModel):
    """Independently recomputed evidence (parsed from the run's artifacts)."""

    plan_step_count: int = Field(ge=0, strict=True)
    subtitle_cue_count: int = Field(ge=0, strict=True)
    dialogue_present: bool
    audio_steps_executed: bool
    color_steps_executed: bool
    color_sections_needed: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = ()
    b_roll_track_present: bool
    primary_track_present: bool
    domain_statuses: dict[str, str]
    blocked_domains: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = ()
    recipe_selection_count: int = Field(ge=0, strict=True)
    provenance_complete: bool
    readback_matched_step_count: int = Field(ge=0, strict=True)
    run_outcome: str = Field(min_length=1, strict=True)
    render_record_present: bool
    publishability_scaffold_present: bool
    publishable: Publishable | None = None
    rollback_transitions: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = ()


def _criterion(requirement: str, *, ok: bool, detail: str) -> GateCriterionV1:
    return GateCriterionV1(requirement=requirement, status="pass" if ok else "fail", detail=detail)


def check_gate(
    report: Episode0FullBuildReportV1, evidence: FullBuildGateEvidenceV1
) -> FullBuildGateCheckV1:
    """Verify the Gate V43-3 machine checklist; never author a pass without proof."""

    domains_explicit = (
        set(report.quality.domain_statuses) == set(SEVEN_DOMAINS)
        and len(report.quality.domain_statuses) == len(SEVEN_DOMAINS)
        and set(report.quality.domain_statuses.values()) <= _VALID_DOMAIN_STATUSES
        and report.quality.domain_statuses == evidence.domain_statuses
    )
    blocked = sorted({*report.quality.blocked_domains, *evidence.blocked_domains})
    provenance_ok = (
        evidence.provenance_complete
        and bool(report.plans.kit_selections)
        and all(
            sel.origin and sel.license and sel.fallback and sel.rationale
            for sel in report.plans.kit_selections
        )
    )
    criteria = (
        _criterion(
            "ir-v2-compiles-to-mcp-execution",
            ok=(
                report.plans.plan_step_count >= 1
                and evidence.plan_step_count == report.plans.plan_step_count
            ),
            detail=(
                f"mcp-execution-plan-v1 steps: report={report.plans.plan_step_count} "
                f"evidence={evidence.plan_step_count} plan_id={report.plans.plan_id[:12]}"
            ),
        ),
        _criterion(
            "subtitles-usable-when-required",
            ok=(not evidence.dialogue_present) or evidence.subtitle_cue_count >= 1,
            detail=(
                f"dialogue_present={evidence.dialogue_present} "
                f"subtitle cues={evidence.subtitle_cue_count}"
            ),
        ),
        _criterion(
            "audio-plan-executed-checked",
            ok=evidence.audio_steps_executed,
            detail=(
                f"audio ladder steps executed={evidence.audio_steps_executed} "
                f"(audio-finishing-plan-v1 sections in the run report)"
            ),
        ),
        _criterion(
            "color-plan-or-justified-noop",
            ok=evidence.color_steps_executed,
            detail=(
                "color-finishing-plan-v1 present (justified no-op is unrepresentable "
                f"at the model); sections needed={evidence.color_sections_needed or 'none'}"
            ),
        ),
        _criterion(
            "broll-primary-coexist",
            ok=evidence.b_roll_track_present and evidence.primary_track_present,
            detail=(
                f"IR v2 tracks: primary={evidence.primary_track_present} "
                f"b_roll={evidence.b_roll_track_present}"
            ),
        ),
        _criterion(
            "seven-domains-explicit",
            ok=domains_explicit,
            detail=f"quality-domain statuses={report.quality.domain_statuses}",
        ),
        _criterion(
            "recipes-resolved-via-kit",
            ok=(
                evidence.recipe_selection_count >= 1
                and evidence.recipe_selection_count == len(report.plans.kit_selections)
            ),
            detail=(
                f"kit selections={evidence.recipe_selection_count} "
                f"recipes={[sel.recipe_id for sel in report.plans.kit_selections]}"
            ),
        ),
        _criterion(
            "recipe-provenance-recorded",
            ok=provenance_ok,
            detail=(
                f"every selection records origin/license/fallback: {evidence.provenance_complete}"
            ),
        ),
        _criterion(
            "no-blocked-domain-at-final",
            ok=not blocked,
            detail=f"blocked domains={blocked or 'none'}",
        ),
        _criterion(
            "readback-source-record-verified",
            ok=(
                evidence.readback_matched_step_count == report.execution.step_count
                and evidence.readback_matched_step_count >= 1
            ),
            detail=(
                f"readback-verified steps={evidence.readback_matched_step_count}/"
                f"{report.execution.step_count}"
            ),
        ),
        _criterion(
            "render-qc-completes",
            ok=(
                evidence.run_outcome == "completed"
                and report.execution.outcome == "completed"
                and evidence.render_record_present
            ),
            detail=(
                f"run outcome={evidence.run_outcome} render record present="
                f"{evidence.render_record_present} (executor={report.executor})"
            ),
        ),
        _criterion(
            "publishability-review-recorded",
            ok=(
                evidence.publishability_scaffold_present
                and (evidence.publishable is None or evidence.publishable in _VALID_PUBLISHABLE)
                and evidence.publishable == report.publishability.publishable
            ),
            detail=(
                "publishability scaffold recorded; publishable="
                f"{evidence.publishable!r} (null = operator-fillable)"
            ),
        ),
        _criterion(
            "legacy-rollback-available",
            ok=(
                report.rollback.checked
                and tuple(evidence.rollback_transitions) == _ROLLBACK_TRANSITIONS
                and report.rollback.transitions == _ROLLBACK_TRANSITIONS
                and report.rollback.legacy_verified_backend == "legacy_direct"
            ),
            detail=(
                f"rollback transitions={list(evidence.rollback_transitions)} "
                f"verified backend={report.rollback.legacy_verified_backend!r}"
            ),
        ),
    )
    return FullBuildGateCheckV1(
        schema_version="episode0-gate-check-v1",
        gate="V43-3",
        run_id=report.run_id,
        passed=all(c.status == "pass" for c in criteria),
        criteria=criteria,
    )


__all__ = [
    "FULL_BUILD_REPORT_SCHEMA",
    "SEVEN_DOMAINS",
    "BuildExecutionV1",
    "BuildKitSelectionV1",
    "BuildPlansV1",
    "BuildPreviewV1",
    "BuildPublishabilityV1",
    "BuildQualityV1",
    "BuildRenderRecordV1",
    "BuildRollbackV1",
    "Episode0FullBuildReportV1",
    "FullBuildGateCheckV1",
    "FullBuildGateEvidenceV1",
    "KitSelectionsArtifactV1",
    "PresentationIntentsArtifactV1",
    "PublishabilityScaffoldV1",
    "check_gate",
]
