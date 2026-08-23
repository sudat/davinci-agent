"""Run orchestration for the T13 finishing harness.

``cmd_run`` drives the full finishing flow: pins/lineage → committed
episode context → kit selections → plans → MCP compile + executor (fake
replay, or the pinned live server probed first — typed blocked, no hang) →
final preview render → QC blocks → domain gate → runtime report. Exit
codes follow the CLI contract: 0 pass / 1 blocked (names on stderr) / the
CLI maps malformed to 2.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast
from uuid import uuid4

from pydantic import ValidationError

from services.cli._v44_finishing_build import (
    FakePlanExecutor,
    FinishingError,
    FinishingMalformedError,
    FinishingPlans,
    execution_facts,
    load_episode_context,
    load_kit_selections,
    mcp_lineage,
)
from services.cli._v44_finishing_ir import ir0c_to_v2
from services.cli._v44_finishing_plans import build_finishing_plans
from services.cli._v44_finishing_qc import (
    ReportInputs,
    assemble_report,
    domain_report,
    editorial_qc,
    technical_qc,
)
from services.cli._v44_finishing_report import REPORT_NAME
from services.cli.episode0 import Episode0BlockedError, _probe_live_executor
from services.cli.preview_render import render_review_preview
from services.cli.review_common import load_tools
from services.config.backends import load_backends, set_backend
from services.creative_plan.quality_domains import ExecutionFactsV1
from services.editorial_v2.model_provider import load_editorial_pin, load_editorial_runtime
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.job_runner.stage_runner import stage_resource
from services.job_runner.stage_runner_models import SequenceClock
from services.job_runner.state_store import StateStore
from services.mcp_client.call_models import McpCallRecorder
from services.mcp_execution.compiler import CompileExecutionPlanError, compile_execution_plan
from services.mcp_execution.runner import McpExecutionRunnerV2
from services.preview.render import PREVIEW_NAME
from services.production_kit.registry import load_kit
from services.toolchain.mcp_pin import McpPinError

if TYPE_CHECKING:
    from services.creative_plan.ir_models_v2 import TimelineIrV2
    from services.mcp_client.execution_runner import McpTransportFn
    from services.mcp_execution.plan_models import McpExecutionPlanV1
    from services.mcp_execution.runner import McpExecutionRunReportV1

EXIT_PASSED: Final = 0
EXIT_BLOCKED: Final = 1
WHISPER_PIN: Final = Path("config/toolchains/pins/whisper-ja.json")
FINISHING_DIR_NAME: Final = "finishing"
JOB_ID: Final = "job-v44-finishing"
STAGE_NAME: Final = "stage-finish"
HOLDER: Final = "v44-finishing"


def load_pins(runtime_path: Path) -> tuple[str, str]:
    """Director pin model id + analysis-provider lineage (strict, no guesses)."""

    try:
        runtime = load_editorial_runtime(runtime_path)
        pin = load_editorial_pin(Path(runtime.director_pin_path))
        whisper = json.loads(WHISPER_PIN.read_bytes())
        provider = f"{whisper['adapter']}:{str(whisper['model']['sha256'])[:12]}"
    except (OSError, ValueError, ValidationError, KeyError, TypeError) as error:
        raise FinishingMalformedError(
            "pins-unreadable",
            f"cannot load model/toolchain pins for lineage ({runtime_path}, "
            f"{WHISPER_PIN}): {error}",
        ) from error
    return pin.model_id, provider


def resolve_executor(
    args: argparse.Namespace, exec_plan: McpExecutionPlanV1
) -> tuple[McpTransportFn, str]:
    if args.executor == "fake":
        return FakePlanExecutor(exec_plan), (
            "fake replay executor (expected readbacks per step; no MCP server, "
            "no Resolve — proof of plan/compile/report wiring only"
        )
    try:
        return _probe_live_executor(args.pin), f"live pinned MCP server (pin={args.pin})"
    except Episode0BlockedError as exc:
        raise FinishingError("mcp-server-unreachable", exc.detail) from exc
    except McpPinError as exc:
        # An unreadable pin is the same operator action as an unreachable
        # server: start/fix the pinned MCP server, then rerun.
        raise FinishingError("mcp-server-unreachable", str(exc)) from exc


def execute_plan(  # noqa: PLR0913 (episode0 _execute_plan wiring, verbatim shape)
    exec_plan: McpExecutionPlanV1,
    *,
    finishing_dir: Path,
    backends_path: Path,
    executor: McpTransportFn,
    executor_name: str,
    run_token: str,
) -> McpExecutionRunReportV1:
    lineage = mcp_lineage()
    store = StateStore.open(finishing_dir / "job-state.sqlite3")
    # Per-run lease keys (T7 D3 uuid discipline): reruns never conflict with
    # a prior invocation's lease in the same finishing dir.
    job_id = f"{JOB_ID}-{run_token}"
    try:
        store.acquire_lease(
            resource=stage_resource(job_id, STAGE_NAME),
            holder=HOLDER,
            now=1000,
            ttl_seconds=600,
        )
        runner = McpExecutionRunnerV2(
            store=store,
            job_id=job_id,
            stage_name=STAGE_NAME,
            holder_token=HOLDER,
            ledger_dir=finishing_dir / "mcp-call-ledger",
            backends_path=backends_path,
            clock=SequenceClock(1000),
        )
        recorder = McpCallRecorder(
            provider_version=lineage["provider_version"],
            resolve_version=lineage["resolve_build"],
            server_mode=executor_name,
            clock=lambda: 0,
        )
        return runner.execute(exec_plan, executor=executor, recorder=recorder)
    finally:
        store.close()


def compile_and_execute(
    args: argparse.Namespace,
    plans: FinishingPlans,
    ir_v2: TimelineIrV2,
    finishing_dir: Path,
) -> tuple[McpExecutionPlanV1, McpExecutionRunReportV1, str]:
    """Compile the MCP plan and execute it; failed capabilities ride their
    fallback matrix rungs automatically (compile loads the real matrix)."""

    audio_plan = plans.audio_plan
    color_plan = plans.color_plan
    if audio_plan is None or color_plan is None:  # pragma: no cover - cmd_run guards
        raise FinishingError("plan-missing", "compile needs both finishing plans")
    try:
        exec_plan = compile_execution_plan(
            ir_v2,
            subtitle_plan=plans.subtitle_plan,
            audio_plan=audio_plan,
            color_plan=color_plan,
            presentation_intents=(),
            kit_selections=dict(plans.recipe_selections),
        )
    except CompileExecutionPlanError as error:
        raise FinishingError(f"compile-{error.code}", error.detail) from error
    atomic_write(finishing_dir / "mcp-execution-plan.json", canonical_model_bytes(exec_plan))
    executor, executor_note = resolve_executor(args, exec_plan)
    previous_backend = load_backends(args.backends).execution_backend
    set_backend("execution_backend", "mcp", path=args.backends)
    try:
        run_report = execute_plan(
            exec_plan,
            finishing_dir=finishing_dir,
            backends_path=args.backends,
            executor=executor,
            executor_name=str(args.executor),
            run_token=uuid4().hex[:8],
        )
    finally:
        set_backend("execution_backend", previous_backend, path=args.backends)
    atomic_write(finishing_dir / "mcp-run-report.json", canonical_model_bytes(run_report))
    return exec_plan, run_report, executor_note


def cmd_run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    director_model_id, analysis_provider = load_pins(args.editorial_runtime)
    ctx = load_episode_context(args.episode_root)
    record = load_kit_selections(args.episode_root)
    ir_v2 = ir0c_to_v2(ctx.ir, ctx.episode_id)
    plans = build_finishing_plans(
        ir_v2, kit=load_kit(), record=record, episode_root=args.episode_root
    )
    finishing_dir = args.episode_root / FINISHING_DIR_NAME
    finishing_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(finishing_dir / "subtitle-plan.json", canonical_model_bytes(plans.subtitle_plan))
    if plans.audio_plan is not None:
        atomic_write(finishing_dir / "audio-plan.json", canonical_model_bytes(plans.audio_plan))
    if plans.color_plan is not None:
        atomic_write(finishing_dir / "color-plan.json", canonical_model_bytes(plans.color_plan))

    plan_compiled = plans.audio_plan is not None and plans.color_plan is not None
    if plan_compiled:
        exec_plan, run_report, executor_note = compile_and_execute(
            args, plans, ir_v2, finishing_dir
        )
    else:
        exec_plan = None
        run_report = None
        executor_note = "plan not compiled — a finishing domain lacks its plan"

    render_review_preview(
        ctx.plan, ctx.ir, ctx.mezzanine, finishing_dir / "final-preview", tools=load_tools()
    )
    preview_path = finishing_dir / "final-preview" / PREVIEW_NAME
    qc_block = technical_qc(
        args.qc_policy, args.render, preview_path, finishing_dir / "qc-report.json"
    )
    editorial_block, _editorial_report = editorial_qc(
        ir_v2, plans.subtitle_plan, plans.audio_plan, finishing_dir / "editorial-qc-report.json"
    )
    inputs = ReportInputs(
        episode_id=ctx.episode_id,
        head_version=ctx.head_version,
        run_id=f"{datetime.now(tz=UTC).date().isoformat()}-finishing",
        executor=cast("Literal['fake', 'live']", str(args.executor)),
        executor_note=executor_note,
        director_model_id=director_model_id,
        analysis_provider=analysis_provider,
        plans=plans,
        plan_compiled=plan_compiled,
        exec_plan=exec_plan,
        run_report=run_report,
        qc_block=qc_block,
        editorial_block=editorial_block,
        preview_path=preview_path,
        preview_sha256=sha256_file(preview_path),
        wall_clock_seconds=round(time.monotonic() - started, 3),
        cue_count=len(ir_v2.subtitle_cues),
        execution_report=(
            execution_facts(run_report, ctx.episode_id)
            if run_report is not None
            else ExecutionFactsV1(episode_id=ctx.episode_id, domains=())
        ),
        notes=(
            *plans.notes,
            f"mcp lineage: {mcp_lineage()}",
            "single-writer: manual CLI run — ensure no cockpit runner is active",
        ),
    )
    quality, gate = domain_report(inputs)
    report = assemble_report(inputs, quality, gate)
    atomic_write(finishing_dir / "quality-domain-report.json", canonical_model_bytes(quality))
    report_path = finishing_dir / REPORT_NAME
    atomic_write(report_path, canonical_model_bytes(report))
    print(f"finishing report: {report_path}")
    print(f"final preview: {preview_path}")
    print(f"gate: {gate.decision}")
    if gate.decision == "reject":
        print(f"blocked domains: {', '.join(gate.blocked_domains)}", file=sys.stderr)
    return EXIT_PASSED if gate.decision == "pass" else EXIT_BLOCKED


__all__ = ["EXIT_BLOCKED", "EXIT_PASSED", "cmd_run", "load_pins"]
