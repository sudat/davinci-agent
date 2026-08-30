"""QC blocks + report assembly for the T13 finishing harness.

Technical QC is REAL when it runs (``run_qc`` over the render with the
supplied policy) and null-with-reason otherwise — a fabricated verdict is
structurally unrepresentable in :class:`FinishingQcBlockV1`. Editorial QC
always runs (no media needed). ``assemble_report`` folds every measured
piece into the runtime ``FinishingRunReportV1``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from services.cli._v44_finishing_report import (
    FinishingEditorialQcBlockV1,
    FinishingKitSelectionV1,
    FinishingQcBlockV1,
    FinishingRunReportV1,
    NativeRenderBlockV1,
)
from services.creative_plan.quality_domains import (
    QualityDomainReportV1,
    QualityFactsV1,
    QualityGateResult,
    QualityPlansV1,
    build_domain_report,
    evaluate_quality_gate,
)
from services.foundation_io import atomic_write, canonical_model_bytes
from services.qc.editorial_checks import (
    EditorialQcInput,
    EditorialQcReportV1,
    aggregate_candidates,
    run_editorial_qc,
)
from services.qc.inputs import OptionalBindings
from services.qc.run import run_qc
from services.qc.tools import QcToolError

if TYPE_CHECKING:
    from services.cli._v44_finishing_build import FinishingPlans
    from services.creative_plan.audio_finishing import AudioFinishingPlanV1
    from services.creative_plan.ir_models_v2 import TimelineIrV2
    from services.creative_plan.quality_domains import ExecutionFactsV1
    from services.creative_plan.subtitle_models import SubtitlePlanV1
    from services.mcp_execution.plan_models import McpExecutionPlanV1
    from services.mcp_execution.runner import McpExecutionRunReportV1
    from services.qc.models import QcReport


def technical_qc(
    policy_path: Path | None,
    render_path: Path | None,
    default_render: Path,
    out_path: Path,
    bindings: OptionalBindings | None = None,
) -> FinishingQcBlockV1:
    """Real QC when it runs; null-with-reason otherwise (never fabricated)."""

    if policy_path is None:
        return FinishingQcBlockV1(
            reason=(
                "no --qc-policy supplied; author one over a clean render via "
                "`python -m services.qc.run build-policy` and rerun"
            )
        )
    target = render_path if render_path is not None else default_render
    if not target.is_file():
        return FinishingQcBlockV1(reason=f"render target missing: {target}")
    try:
        report: QcReport = run_qc(
            target, policy_path, out_path, bindings or OptionalBindings()
        )
    except (OSError, ValueError, ValidationError, QcToolError) as error:
        return FinishingQcBlockV1(reason=f"qc run failed: {error}")
    return FinishingQcBlockV1(
        verdict=report.verdict,
        issue_count=len(report.issues),
        blocker_count=sum(1 for issue in report.issues if issue.severity == "blocker"),
        unresolved_gate_count=len(report.unresolved_human_gates),
        report_path=str(out_path),
    )


def editorial_qc(
    ir_v2: TimelineIrV2,
    subtitle_plan: SubtitlePlanV1,
    audio_plan: AudioFinishingPlanV1 | None,
    out_path: Path,
) -> tuple[FinishingEditorialQcBlockV1, EditorialQcReportV1]:
    candidates = run_editorial_qc(
        EditorialQcInput(ir_v2=ir_v2, subtitle_plan=subtitle_plan, audio_plan=audio_plan)
    )
    report = aggregate_candidates(candidates)
    atomic_write(out_path, canonical_model_bytes(report))
    return (
        FinishingEditorialQcBlockV1(
            candidate_count=len(report.candidates),
            critical_count=report.counts_by_severity["critical"],
            needs_review_count=sum(1 for c in candidates if c.needs_human_review),
            report_path=str(out_path),
        ),
        report,
    )


def quality_facts(
    episode_id: str,
    head_version: int,
    cue_count: int,
    run_report: McpExecutionRunReportV1 | None,
    *,
    qc_verdict_passed: bool,
) -> QualityFactsV1:
    delivery_passed = (
        run_report is not None and run_report.outcome == "completed" and qc_verdict_passed
    )
    evidence = (
        f"review-store head v{head_version}: committed edit plan + IR",
        f"committed subtitle cues: {cue_count}",
    )
    return QualityFactsV1(
        episode_id=episode_id,
        has_dialogue=cue_count > 0,
        editorial_blocking_defect=False,
        editorial_evidence=evidence,
        framing_motion_intended=False,
        graphics_intended=False,
        delivery_qc_passed=delivery_passed,
        delivery_qc_evidence=(
            (
                f"mcp-execution-run-report-v1: outcome={run_report.outcome}",
                "qc-report-v1: verdict=passed",
            )
            if delivery_passed
            and run_report is not None  # narrowed for the type checker
            else ()
        ),
    )


@dataclass(frozen=True, slots=True)
class ReportInputs:
    """Every measured piece the runtime report folds together."""

    episode_id: str
    head_version: int
    run_id: str
    executor: Literal["fake", "live"]
    executor_note: str
    director_model_id: str
    analysis_provider: str
    plans: FinishingPlans
    plan_compiled: bool
    exec_plan: McpExecutionPlanV1 | None
    run_report: McpExecutionRunReportV1 | None
    qc_block: FinishingQcBlockV1
    editorial_block: FinishingEditorialQcBlockV1
    preview_path: Path
    preview_sha256: str
    wall_clock_seconds: float
    cue_count: int
    execution_report: ExecutionFactsV1
    notes: tuple[str, ...]
    native_render: NativeRenderBlockV1 | None = None


def domain_report(inputs: ReportInputs) -> tuple[QualityDomainReportV1, QualityGateResult]:
    quality = build_domain_report(
        quality_facts(
            inputs.episode_id,
            inputs.head_version,
            inputs.cue_count,
            inputs.run_report,
            qc_verdict_passed=inputs.qc_block.verdict == "passed",
        ),
        plans=QualityPlansV1(
            subtitle_plan=inputs.plans.subtitle_plan,
            audio_plan=inputs.plans.audio_plan,
            color_plan=inputs.plans.color_plan,
        ),
        execution_report=inputs.execution_report,
    )
    return quality, evaluate_quality_gate(quality)


def assemble_report(
    inputs: ReportInputs,
    quality: QualityDomainReportV1,
    gate: QualityGateResult,
) -> FinishingRunReportV1:
    run_report = inputs.run_report
    exec_plan = inputs.exec_plan
    return FinishingRunReportV1(
        schema_version="v44-finishing-run-v1",
        episode_id=inputs.episode_id,
        run_id=inputs.run_id,
        executor=inputs.executor,
        executor_note=inputs.executor_note,
        director_model_id=inputs.director_model_id,
        analysis_provider=inputs.analysis_provider,
        review_head_version=inputs.head_version,
        kit_selections=tuple(
            FinishingKitSelectionV1.model_validate(entry) for entry in inputs.plans.kit_snapshot
        ),
        plan_compiled=inputs.plan_compiled,
        plan_id=exec_plan.plan_id if exec_plan is not None else None,
        plan_step_count=len(exec_plan.steps) if exec_plan is not None else None,
        execution_outcome=run_report.outcome if run_report is not None else None,
        completed_step_count=(
            sum(1 for s in run_report.steps if s.status == "completed")
            if run_report is not None
            else None
        ),
        failed_step_count=(
            sum(1 for s in run_report.steps if s.status == "failed")
            if run_report is not None
            else None
        ),
        fallback_rung_count=len(run_report.rung_entries) if run_report is not None else None,
        domain_statuses={entry.domain: entry.status for entry in quality.domains},
        domain_justifications={
            entry.domain: entry.justification
            for entry in quality.domains
            if entry.justification is not None
        },
        blocked_domains=gate.blocked_domains,
        surfaced_manual_items=gate.surfaced_manual_items,
        gate_decision=gate.decision,
        technical_qc=inputs.qc_block,
        editorial_qc=inputs.editorial_block,
        final_preview_path=str(inputs.preview_path),
        final_preview_sha256=inputs.preview_sha256,
        native_render=inputs.native_render,
        wall_clock_seconds=inputs.wall_clock_seconds,
        notes=inputs.notes,
    )



__all__ = [
    "ReportInputs",
    "assemble_report",
    "domain_report",
    "editorial_qc",
    "quality_facts",
    "technical_qc",
]
