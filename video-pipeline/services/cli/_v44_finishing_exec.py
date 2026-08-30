"""Executor wiring extracted from _v44_finishing_run (split for 250 LOC ceiling)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from pydantic import ValidationError

from services.cli._v44_finishing_build import (
    FakePlanExecutor,
    FinishingError,
    FinishingMalformedError,
    FinishingPlans,
    mcp_lineage,
)
from services.cli.episode0 import Episode0BlockedError, _probe_live_executor
from services.config.backends import load_backends, set_backend
from services.editorial_v2.model_provider import load_editorial_pin, load_editorial_runtime
from services.foundation_io import atomic_write, canonical_model_bytes
from services.job_runner.stage_runner import stage_resource
from services.job_runner.stage_runner_models import SequenceClock
from services.job_runner.state_store import StateStore
from services.mcp_client.call_models import McpCallRecorder
from services.mcp_execution.audio_measurement import default_audio_measurement
from services.mcp_execution.color_measurement import default_frame_comparison
from services.mcp_execution.compiler import CompileExecutionPlanError, compile_execution_plan
from services.mcp_execution.live_adapter import LiveMcpAdapter
from services.mcp_execution.live_errors import LiveAdapterError
from services.mcp_execution.native_render_media import load_ffprobe, probe_video_frame_count
from services.mcp_execution.runner import McpExecutionRunnerV2
from services.toolchain.mcp_pin import McpPinError

if TYPE_CHECKING:
    from services.creative_plan.ir_models_v2 import TimelineIrV2
    from services.mcp_client.execution_runner import McpTransportFn
    from services.mcp_execution.plan_models import McpExecutionPlanV1
    from services.mcp_execution.runner import McpExecutionRunReportV1

WHISPER_PIN: Final = Path("config/toolchains/pins/whisper-ja.json")
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


def _media_frame_counts(media_paths: Mapping[str, str] | None) -> dict[str, int]:
    """Trusted per-source media frame EOFs (ffprobe ``nb_frames`` over the
    actual imported media files — never inferred from plan spans)."""

    raw = dict(media_paths or {})
    if not raw:
        return {}
    ffprobe = load_ffprobe()
    by_path = {
        path: probe_video_frame_count(Path(path), ffprobe)
        for path in sorted(set(raw.values()))
    }
    return {source_id: by_path[path] for source_id, path in raw.items()}


def resolve_executor(  # noqa: PLR0913
    args: argparse.Namespace,
    exec_plan: McpExecutionPlanV1,
    media_paths: Mapping[str, str] | None = None,
    *,
    audio_measure: object | None = None,
    render_dir: str | None = None,
    frame_diff: object | None = None,
) -> tuple[McpTransportFn, str]:
    if args.executor == "fake":
        import sys as _sys  # noqa: PLC0415

        _run_mod = _sys.modules.get("services.cli._v44_finishing_run")
        _fake_cls = getattr(_run_mod, "FakePlanExecutor", None) if _run_mod is not None else None
        _cls = (  # type: ignore[comparison-overlap]
            _fake_cls
            if _fake_cls is not None and _fake_cls is not FakePlanExecutor
            else FakePlanExecutor
        )
        return _cls(exec_plan), (  # type: ignore[operator]
            "fake replay executor (expected readbacks per step; no MCP server, "
            "no Resolve — proof of plan/compile/report wiring only"
        )
    try:
        frame_counts = _media_frame_counts(media_paths)
    except LiveAdapterError as exc:
        raise FinishingError(
            "media-frame-count-unavailable",
            f"cannot establish media frame EOFs for placement reconciliation "
            f"({exc.code}: {exc.detail})",
        ) from exc
    try:
        import sys as _sys  # noqa: PLC0415

        _run_mod = _sys.modules.get("services.cli._v44_finishing_run")
        _probe = getattr(_run_mod, "_probe_live_executor", None) if _run_mod is not None else None
        _probe_fn = (  # type: ignore[attr-defined]
            _probe
            if _probe is not None and _probe is not _probe_live_executor
            else _probe_live_executor
        )
        raw = _probe_fn(args.pin)
        live: McpTransportFn = LiveMcpAdapter(
            raw,
            media_paths=media_paths or {},
            media_frame_counts=frame_counts,
            audio_measure=audio_measure,  # type: ignore[arg-type]
            render_dir=render_dir,
            frame_diff=frame_diff,  # type: ignore[arg-type]
        )
        return live, f"live pinned MCP server (pin={args.pin})"  # noqa: TRY300
    except Episode0BlockedError as exc:
        raise FinishingError("mcp-server-unreachable", exc.detail) from exc
    except McpPinError as exc:
        raise FinishingError("mcp-server-unreachable", str(exc)) from exc


def execute_plan(  # noqa: PLR0913
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
    media_paths: Mapping[str, str] | None = None,
) -> tuple[McpExecutionPlanV1, McpExecutionRunReportV1, str]:
    """Compile the MCP plan and execute it; failed capabilities ride fallback rungs."""

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
    executor, executor_note = resolve_executor(
        args,
        exec_plan,
        media_paths,
        audio_measure=default_audio_measurement(),
        render_dir=str(finishing_dir / "render-audio"),
        frame_diff=default_frame_comparison(),
    )
    previous_backend = load_backends(args.backends).execution_backend
    set_backend("execution_backend", "mcp", path=args.backends)
    try:
        import sys as _sys  # noqa: PLC0415 (test monkeypatch seam)

        _run_mod = _sys.modules.get("services.cli._v44_finishing_run")
        _exec_fn = (  # type: ignore[attr-defined]
            _run_mod.execute_plan
            if _run_mod is not None and getattr(_run_mod, "execute_plan", None) is not execute_plan
            else execute_plan
        )
        run_report = _exec_fn(
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


__all__ = [
    "HOLDER",
    "JOB_ID",
    "STAGE_NAME",
    "compile_and_execute",
    "execute_plan",
    "load_pins",
    "resolve_executor",
]
