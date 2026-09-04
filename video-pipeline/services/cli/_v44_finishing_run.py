"""Run orchestration for the T13 finishing harness (split for LOC ceiling).

``cmd_run`` drives pins/lineage → plans → MCP compile/executor → native
Resolve render (live) or local preview (fake) → QC → gate → report.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, cast

from pydantic import ValidationError

from services.cli._v44_finishing_build import (
    FinishingError,
    execution_facts,
    load_episode_context,
    load_kit_selections,
    mcp_lineage,
)
from services.cli._v44_finishing_exec import compile_and_execute
from services.cli._v44_finishing_ir import ir0c_to_v2
from services.cli._v44_finishing_plans import build_finishing_plans, load_audio_facts
from services.cli._v44_finishing_qc import (
    ReportInputs,
    assemble_report,
    domain_report,
    editorial_qc,
    technical_qc,
)
from services.cli._v44_finishing_report import REPORT_NAME, NativeRenderBlockV1
from services.cli._v44_orientation_bindings import orientation_bindings
from services.cli.preview_render import render_review_preview
from services.cli.review_common import load_tools
from services.creative_plan.quality_domains import ExecutionFactsV1
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.mcp_execution.native_render_media import (
    load_ffprobe,
    parse_frame_rate,
    validate_native_media,
)
from services.mcp_execution.native_render_meta import NativeRenderMeta
from services.preview.render import PREVIEW_NAME
from services.production_kit.registry import load_kit

EXIT_PASSED: Final = 0
EXIT_BLOCKED: Final = 1
FINISHING_DIR_NAME: Final = "finishing"

# Re-export for tests that patched _v44_finishing_run internals.
from services.cli._v44_finishing_exec import (  # noqa: F401,E402
    HOLDER,
    JOB_ID,
    STAGE_NAME,
    WHISPER_PIN,
    FakePlanExecutor,
    execute_plan,
    load_pins,
    resolve_executor,
)
from services.cli.episode0 import _probe_live_executor  # noqa: F401,E402


def _load_native_render(  # noqa: PLR0911 (each reconciliation gate is its own refusal)
    finishing_dir: Path, episode_id: str
) -> tuple[Path, str, NativeRenderBlockV1] | None:
    """Reconcile the runner's persisted native-render record.

    The record must name the exact deterministic custom name and output
    path; EVERY measured fact is then independently re-measured with the
    pinned ffprobe and must equal the record exactly (editable metadata
    cannot inject report values). The report block is built from the
    RE-MEASURED facts. Any absence/malformation/drift yields None.
    """

    render_dir = finishing_dir / "final-resolve-render"
    expected_name = f"finishing-native-{episode_id}"
    meta_path = render_dir / f"{expected_name}.meta.json"
    expected_out = render_dir / f"{expected_name}.mp4"
    if not meta_path.is_file():
        return None
    try:
        meta = NativeRenderMeta.model_validate_json(meta_path.read_bytes())
    except (OSError, ValidationError):
        return None
    if meta.custom_name != expected_name:
        return None
    if Path(meta.output_path) != expected_out:
        return None
    if not expected_out.is_file() or expected_out.stat().st_size == 0:
        return None
    if sha256_file(expected_out) != meta.output_sha256:
        return None
    try:
        probe = validate_native_media(
            expected_out,
            load_ffprobe(),
            expected_width=meta.width,
            expected_height=meta.height,
            expected_fps=parse_frame_rate(meta.avg_frame_rate),
            expected_video_codec=meta.video_codec,
            expected_audio_codec=meta.audio_codec,
            expected_channels=meta.audio_channels,
            expected_sample_rate=meta.audio_sample_rate,
        )
    except Exception:  # noqa: BLE001 — any drift/corruption is honest None
        return None
    if (
        probe.video_codec.lower() != meta.video_codec.lower()
        or probe.audio_codec.lower() != meta.audio_codec.lower()
        or probe.width != meta.width
        or probe.height != meta.height
        or probe.avg_frame_rate != meta.avg_frame_rate
        or probe.audio_channels != meta.audio_channels
        or probe.audio_sample_rate != meta.audio_sample_rate
        or probe.duration_seconds != meta.duration_seconds
        or probe.has_subtitle_stream != meta.has_subtitle_stream
    ):
        return None
    block = NativeRenderBlockV1(
        job_id=meta.job_id,
        output_path=str(expected_out),
        output_sha256=meta.output_sha256,
        output_size_bytes=expected_out.stat().st_size,
        duration_seconds=probe.duration_seconds,
        video_codec=probe.video_codec,
        width=probe.width,
        height=probe.height,
        audio_codec=probe.audio_codec,
        audio_channels=probe.audio_channels,
    )
    return expected_out, meta.output_sha256, block


def cmd_run(args) -> int:  # type: ignore[no-untyped-def]  # noqa: ANN001, C901, PLR0912, PLR0915
    started = time.monotonic()
    from services.cli._v44_finishing_exec import load_pins as _load_pins  # noqa: PLC0415

    director_model_id, analysis_provider = _load_pins(args.editorial_runtime)
    ctx = load_episode_context(args.episode_root)
    record = load_kit_selections(args.episode_root)
    ir_v2 = ir0c_to_v2(ctx.ir, ctx.episode_id)
    plans = build_finishing_plans(
        ir_v2,
        kit=load_kit(),
        record=record,
        episode_root=args.episode_root,
        audio_facts=load_audio_facts(args.audio_facts),
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
        _source_ids: set[str] = set()
        for _track in (*ir_v2.video_tracks, *ir_v2.audio_tracks):
            for _item in _track.items:
                _sid = _item.source.source_id
                if not _sid.startswith("still-") and not _sid.startswith("graphic-"):
                    _source_ids.add(_sid)
        _media_paths: dict[str, str] = {sid: str(ctx.mezzanine) for sid in _source_ids}
        exec_plan, run_report, executor_note = compile_and_execute(
            args, plans, ir_v2, finishing_dir, _media_paths
        )
    else:
        exec_plan = None
        run_report = None
        executor_note = "plan not compiled — a finishing domain lacks its plan"

    if run_report is not None and run_report.outcome == "failed":
        failed = [s for s in run_report.steps if s.status == "failed"]
        first = failed[0] if failed else None
        first_id = first.step_id if first is not None else "unknown"
        first_action = first.action if first is not None else "unknown"
        first_code = (
            first.failure_code
            if first is not None and first.failure_code is not None
            else "unknown"
        )
        raise FinishingError(
            "mcp-execution-failed",
            f"mcp execution failed: {len(failed)} step(s) failed; "
            f"first {first_id}/{first_action} code={first_code} — BLOCKED",
        )

    preview_path: Path
    preview_sha: str
    native_block: NativeRenderBlockV1 | None = None
    loaded = _load_native_render(finishing_dir, ctx.episode_id)
    is_live_ok = (
        str(args.executor) == "live"
        and run_report is not None
        and run_report.outcome == "completed"
    )
    if loaded is not None and is_live_ok:
        preview_path, preview_sha, native_block = loaded
    elif is_live_ok and loaded is None:
        raise FinishingError(
            "render-native-missing",
            "native render step completed but no valid output was reconciled",
        )
    else:
        render_review_preview(
            ctx.plan, ctx.ir, ctx.mezzanine, finishing_dir / "final-preview", tools=load_tools()
        )
        preview_path = finishing_dir / "final-preview" / PREVIEW_NAME
        preview_sha = sha256_file(preview_path)

    # Technical QC on the same file the report points at.
    qc_block = technical_qc(
        args.qc_policy,
        args.render,
        preview_path,
        finishing_dir / "qc-report.json",
        bindings=orientation_bindings(finishing_dir, args.episode_root, ctx),
    )
    editorial_block, _editorial_report = editorial_qc(
        ir_v2,
        plans.subtitle_plan,
        plans.audio_plan,
        finishing_dir / "editorial-qc-report.json",
    )
    from services.cli._v44_domain_evidence import (  # noqa: PLC0415
        load_episode_domain_evidence,
        probe_geometry,
    )

    domain_evidence = load_episode_domain_evidence(
        args.episode_root,
        protocol_path=args.episode_protocol,
        episode_id=ctx.episode_id,
        ir_graphics_items=sum(
            len(track.items)
            for track in ir_v2.video_tracks
            if track.role in ("still", "graphic")
        ),
        mezzanine_geometry=probe_geometry(ctx.mezzanine, load_tools().ffprobe),
        cue_count=len(ir_v2.subtitle_cues),
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
        preview_sha256=preview_sha,
        native_render=native_block,
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
        evidence=domain_evidence,
    )
    quality, gate = domain_report(inputs)
    report = assemble_report(inputs, quality, gate)
    atomic_write(finishing_dir / "quality-domain-report.json", canonical_model_bytes(quality))
    report_path = finishing_dir / REPORT_NAME
    atomic_write(report_path, canonical_model_bytes(report))
    print(f"finishing report: {report_path}")
    print(f"final preview: {preview_path}")
    if native_block is not None:
        print(f"native render: {native_block.output_path} job={native_block.job_id}")
    print(f"gate: {gate.decision}")
    if gate.decision == "reject":
        print(f"blocked domains: {', '.join(gate.blocked_domains)}", file=sys.stderr)
    return EXIT_PASSED if gate.decision == "pass" else EXIT_BLOCKED


__all__ = ["EXIT_BLOCKED", "EXIT_PASSED", "cmd_run", "load_pins"]
