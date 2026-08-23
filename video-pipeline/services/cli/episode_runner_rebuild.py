"""Stage-subset re-entry for applied review commands (task 9).

``--from-stage`` executes the stop-bounded lineage projection: the stages
from the given re-entry point up to ``preview`` (the ``PREVIEW_READY``
stop) run against the CURRENT committed artifacts — the cockpit review
store under ``review/`` holds the plan version the deterministic
apply/commit path sealed, so the plan stage consumes that head version,
compile materializes its IR, and preview re-renders + re-publishes the
operator preview. Stages before the re-entry point are untouched; lineage
stages beyond ``PREVIEW_READY`` (resolve_build/qc/render) are skipped with
a log line naming the stop bound. The chain's own job store under ``run/``
is not regressed; the cockpit job keeps its status and the re-entry
appends fresh per-run stage rows (uuid idempotency keys — the task-7
mirror discipline).
"""

# allow: SIZE_OK — 273 pure LOC: plan-pinned single-file re-entry executor
# (stage-set projection + plan/compile/preview stage fns + bundle hand-off +
# metrics writer belong to one task-9 commit scope); same precedent as
# review_chat.py / review_interpreter.py.

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final

from pydantic import Field

from services.cli.bundle import ReviewBundle, ReviewTarget, load_bundle, save_bundle
from services.cli.episode_runner_state import RunContext, block_stage, log_event, record_stage
from services.cli.episode_runner_workspace import (
    COCKPIT_REVIEW_LOG_RELATIVE,
    COCKPIT_REVIEW_STORE_RELATIVE,
    RUN_DIR_NAME,
    publish_preview,
)
from services.cli.preview_render import render_review_preview
from services.cli.project import plan_sha256
from services.cli.review_common import (
    load_tools,
    mezzanine_for,
    previous_trace,
    store_ir,
    store_plan,
)
from services.contracts.primitives import StrictModel
from services.episode_cockpit.review_chat import PIPELINE_STAGES
from services.foundation_io import sha256_file
from services.preview.models import AppliedDecision
from services.preview.render import PREVIEW_NAME, TRACE_NAME
from services.review_command.store import HeadState, load_head

if TYPE_CHECKING:
    from services.cli.episode_runner import RunnerInvocation
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C
    from services.job_runner.state_store import StateStore

REENTRY_FROM_STAGES: Final = ("selection", "plan", "compile", "preview")
STOP_STAGE: Final = "preview"
BEYOND_STOP_STAGES: Final = ("resolve_build", "qc", "render")
REQUIRED_STATUS: Final = "PREVIEW_READY"
METRICS_NAME: Final = "rebuild-metrics.jsonl"
BUNDLE_NAME: Final = "review-bundle.json"


class RebuildStageError(Exception):
    """Typed blocked-refusal carrying the operator-actionable reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class RebuildMetricV1(StrictModel):
    """One append-only rebuild measurement line (plan §7.4).

    ``interpretation_ms`` stays null on this writer: interpretation
    happened on the apply route, which does not report timing; the single
    ``confirmations`` count is the operator's apply confirmation the
    rebuild derives from (deterministic drafts need no extra rounds).
    """

    schema_version: str = Field(default="cockpit-rebuild-metric-v1", strict=True)
    sequence: int = Field(ge=1, strict=True)
    applied_command: str = Field(min_length=1, strict=True)
    stages: tuple[str, ...]
    interpretation_ms: float | None = None
    rebuild_wall_clock_seconds: float = Field(ge=0.0, strict=True)
    unrelated_stages_skipped: tuple[str, ...]
    confirmations: int = Field(ge=0, strict=True)
    at: str = Field(min_length=1, strict=True)


@dataclass(frozen=True, slots=True)
class ReentryStages:
    executed: tuple[str, ...]
    skipped: tuple[str, ...]


@dataclass
class ReentryState:
    """Artifacts carried across the re-entry stages (head, then plan+IR)."""

    head: HeadState | None = None
    plan: EditPlan0C | None = None
    ir: TimelineIr0C | None = None


def reentry_stages(from_stage: str) -> ReentryStages:
    """Stop-bounded lineage projection: [from_stage .. preview] of PIPELINE_STAGES."""

    if from_stage not in REENTRY_FROM_STAGES:
        raise RebuildStageError(
            "from-stage-unsupported",
            f"--from-stage must be one of {REENTRY_FROM_STAGES}, got {from_stage!r}",
        )
    start = PIPELINE_STAGES.index(from_stage)
    stop = PIPELINE_STAGES.index(STOP_STAGE)
    executed = tuple(PIPELINE_STAGES[start : stop + 1])
    return ReentryStages(
        executed=executed,
        skipped=tuple(stage for stage in PIPELINE_STAGES if stage not in executed),
    )


def stage_plan(episode_root: Path) -> HeadState:
    """Consume the LATEST committed plan version (stale-state safe)."""

    log_path, plan_dir = _review_store(episode_root)
    try:
        return load_head(log_path, plan_dir)
    except Exception as error:
        raise RebuildStageError("review-store-unreadable", str(error)) from error


def stage_compile(episode_root: Path, head: HeadState) -> tuple[EditPlan0C, TimelineIr0C]:
    """Materialize the head version's plan + IR (hash-verified inside load_head)."""

    _, plan_dir = _review_store(episode_root)
    try:
        return (
            store_plan(plan_dir / f"plan-v{head.version}.json"),
            store_ir(plan_dir / f"ir-v{head.version}.json"),
        )
    except Exception as error:
        raise RebuildStageError("ir-unreadable", str(error)) from error


def stage_preview(
    episode_root: Path, head: HeadState, plan: EditPlan0C, ir: TimelineIr0C, log: BinaryIO
) -> str:
    """Re-render the review-plane preview from the new version + republish."""

    run_dir = episode_root / RUN_DIR_NAME
    bundle_file = run_dir / BUNDLE_NAME
    try:
        bundle = load_bundle(bundle_file)
        preview_dir = run_dir / f"preview-v{head.version}"
        decision = AppliedDecision(
            decision_id=f"decision-rebuild-{bundle.episode_id}-v{head.version}",
            case_id=bundle.episode_id,
            classification="clear",
            plan_version_after=f"v{head.version}",
            previous_trace=previous_trace(bundle_file, bundle),
        )
        render_review_preview(
            plan, ir, mezzanine_for(bundle_file, bundle), preview_dir,
            tools=load_tools(), decision=decision,
        )
    except Exception as error:
        raise RebuildStageError("preview-failed", str(error)) from error
    preview_sha = sha256_file(run_dir / f"preview-v{head.version}" / PREVIEW_NAME)
    _update_bundle(bundle_file, bundle, head, preview_sha)
    publish_preview(episode_root, log, source_dir=f"preview-v{head.version}")
    return preview_sha


def _review_store(episode_root: Path) -> tuple[Path, Path]:
    return (
        episode_root.joinpath(*COCKPIT_REVIEW_LOG_RELATIVE),
        episode_root.joinpath(*COCKPIT_REVIEW_STORE_RELATIVE),
    )


def _update_bundle(
    bundle_file: Path, bundle: ReviewBundle, head: HeadState, preview_sha: str
) -> None:
    """Point the review bundle at the rebuilt head (store paths → cockpit layout)."""

    log_path, plan_dir = _review_store(bundle_file.parent.parent)
    preview_dir = f"preview-v{head.version}"
    target = ReviewTarget(
        plan_version=f"v{head.version}",
        plan_sha256=sha256_file(plan_dir / f"plan-v{head.version}.json"),
        ir_sha256=sha256_file(plan_dir / f"ir-v{head.version}.json"),
        preview_dir=preview_dir,
        preview_sha256=preview_sha,
        trace_sha256=sha256_file(bundle_file.parent / preview_dir / TRACE_NAME),
    )
    applied = tuple(
        dict.fromkeys(
            (
                *bundle.applied_event_ids,
                *(event.event_id for event in head.events if event.kind == "decision_applied"),
            )
        )
    )
    save_bundle(
        bundle.model_copy(
            update={
                "store_dir": plan_dir.relative_to(bundle_file.parent).as_posix(),
                "events_log": log_path.relative_to(bundle_file.parent).as_posix(),
                "current": target,
                "applied_event_ids": applied,
            }
        ),
        bundle_file,
    )


def _execute(
    store: StateStore, ctx: RunContext, episode_root: Path, stages: ReentryStages
) -> None:
    carried = ReentryState()
    for stage in stages.executed:
        record_stage(store, ctx, stage, "running")
        log_event(ctx.log, "rebuild_stage", stage=stage, status="running")
        try:
            adopted = _run_stage(stage, episode_root, carried, ctx.log)
        except RebuildStageError as error:
            block_stage(store, ctx, stage, error.code)
            log_event(
                ctx.log, "rebuild_stage", stage=stage, status="failed_blocked", code=error.code
            )
            raise
        record_stage(store, ctx, stage, "succeeded", adopted=adopted)
        log_event(ctx.log, "rebuild_stage", stage=stage, status="succeeded")


def _run_stage(stage: str, episode_root: Path, carried: ReentryState, log: BinaryIO) -> str:
    """One re-entry stage against the carried state; returns its adopted hash.

    Entering at compile/preview hydrates the missing head/IR itself (the
    review store is an idempotent read), so every legal ``--from-stage``
    value is self-sufficient without re-running earlier stages.
    """

    if stage == "selection":
        raise RebuildStageError(
            "stage-reentry-unsupported",
            "selection re-entry needs the director pass; not rebuild-executable yet",
        )
    if stage == "plan":
        carried.head = stage_plan(episode_root)
        return plan_sha256(carried.head.plan)
    if carried.head is None:
        carried.head = stage_plan(episode_root)
    if stage == "compile":
        carried.plan, carried.ir = stage_compile(episode_root, carried.head)
        return carried.head.index.versions[str(carried.head.version)].ir_sha256
    if carried.plan is None or carried.ir is None:
        carried.plan, carried.ir = stage_compile(episode_root, carried.head)
    return stage_preview(episode_root, carried.head, carried.plan, carried.ir, log)


def run_reentry(store: StateStore, ctx: RunContext, call: RunnerInvocation, log: BinaryIO) -> int:
    """Execute one validated --from-stage re-entry; returns the exit code."""

    from services.cli.episode_runner import EXIT_SUCCESS  # noqa: PLC0415 (avoids import cycle)

    stages = reentry_stages(call.from_stage if call.from_stage is not None else "")
    for stage in BEYOND_STOP_STAGES:
        log_event(log, "rebuild_stage_skipped", stage=stage, reason=f"beyond {call.stop} stop")
    snapshot = store.get_job_snapshot(ctx.job_id)
    if snapshot.job.status != REQUIRED_STATUS:
        raise RebuildStageError(
            "episode-not-preview-ready",
            f"rebuild re-entry needs {REQUIRED_STATUS}, job is at {snapshot.job.status}",
        )
    started = time.monotonic()
    _execute(store, ctx, call.episode_root, stages)
    wall = time.monotonic() - started
    if call.applied_command is not None:
        _append_metric(call.episode_root, call.applied_command, stages, wall)
    log_event(log, "rebuild_finished", stages=list(stages.executed), wall_seconds=round(wall, 3))
    return EXIT_SUCCESS


def _append_metric(
    episode_root: Path, applied_command: str, stages: ReentryStages, wall: float
) -> None:
    metrics_path = episode_root / METRICS_NAME
    sequence = (
        sum(1 for line in metrics_path.read_bytes().splitlines() if line.strip()) + 1
        if metrics_path.is_file()
        else 1
    )
    metric = RebuildMetricV1(
        sequence=sequence,
        applied_command=applied_command,
        stages=stages.executed,
        rebuild_wall_clock_seconds=round(wall, 3),
        unrelated_stages_skipped=stages.skipped,
        confirmations=1,
        at=datetime.now(UTC).isoformat(),
    )
    with metrics_path.open("ab") as stream:
        stream.write(metric.model_dump_json().encode() + b"\n")


__all__ = [
    "BEYOND_STOP_STAGES",
    "METRICS_NAME",
    "REENTRY_FROM_STAGES",
    "REQUIRED_STATUS",
    "RebuildMetricV1",
    "RebuildStageError",
    "reentry_stages",
    "run_reentry",
    "stage_compile",
    "stage_plan",
    "stage_preview",
]
