"""Native render lifecycle handler for the execution plan (Task 7).

Orchestration only: preflight (render_poll), trusted rerun/resume
(render_resume), format/codec pin + per-setting apply (render_settings),
then queue/start the single job and hand polling back to render_poll.
Media validation lives at the non-handler boundary
``services.mcp_execution.native_render_media`` so the finishing CLI can
reconcile through the same strict seam without importing handlers.
"""

from __future__ import annotations

import time
from pathlib import Path

from services.foundation_io import sha256_file
from services.mcp_client.ops_models import AddJobResult, McpActionOutcome
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveSessionContext,
    validate_params,
)
from services.mcp_execution.live_handlers.render_poll import (
    assert_render_stopped,
    wait_for_completion,
)
from services.mcp_execution.live_handlers.render_resume import (
    discard_untrusted,
    persist_meta,
    trusted_reuse,
)
from services.mcp_execution.live_handlers.render_settings import (
    per_setting_apply,
    verify_format_codec,
)
from services.mcp_execution.native_render_media import load_ffprobe, validate_native_media
from services.mcp_execution.plan_payloads import RenderNativeParams


def _target_dir(ctx: LiveSessionContext) -> Path:
    if ctx.render_dir is None:
        raise LiveAdapterError("render-target-unconfigured", "render_dir missing")
    base = Path(str(ctx.render_dir))
    if base.name == "render-audio":
        return base.parent / "final-resolve-render"
    return base


def render_native(  # noqa: C901 (linear lifecycle gates; split modules own the branches)
    ctx: LiveSessionContext, _action: str, params: dict[str, object] | object
) -> dict[str, object]:
    p = validate_params(RenderNativeParams, params if isinstance(params, dict) else {})  # type: ignore[arg-type]
    target = _target_dir(ctx)
    target.mkdir(parents=True, exist_ok=True)

    # Before queuing OR reusing: prove nothing is rendering. The pinned MCP
    # global state action is called unconditionally; listed jobs refine it.
    assert_render_stopped(ctx)

    # Render currency (Task 8): a trusted prior render is reusable only
    # when THIS session did not mutate timeline content after that render
    # existed — otherwise the "reused" output predates the current
    # timeline (measured hazard: color grades applied in-run would never
    # appear in a reused render).
    reused = None if ctx.timeline_mutated else trusted_reuse(target, p)
    if reused is not None:
        return reused
    discard_untrusted(target, p.custom_name)

    verify_format_codec(ctx, p.format_id, p.codec_id)

    ordered: dict[str, object] = {
        "TargetDir": str(target),
        "CustomName": p.custom_name,
        "SelectAllFrames": p.select_all_frames,
        "ExportVideo": p.export_video,
        "ExportAudio": p.export_audio,
        "DataBurnIn": p.data_burn_in,
        "FormatWidth": p.width,
        "FormatHeight": p.height,
        "FrameRate": p.frame_rate,
    }
    applied = per_setting_apply(ctx, ordered)
    for row in applied:
        if row["key"] == "TargetDir" and row["value"] != str(target):
            raise LiveAdapterError(
                "render-setting-target-mismatch",
                f"TargetDir echo {row['value']!r} != {str(target)!r}",
            )

    job = AddJobResult.model_validate(
        ctx.transport(
            "render",
            "prepare_render_job",
            {
                "target_dir": str(target),
                "custom_name": p.custom_name,
                "format": p.format_id,
                "codec": p.codec_id,
                "require_temp_target": False,
                "settings": ordered,
            },
        )
    )
    if not job.ok or not job.job_id:
        raise LiveAdapterError("render-job-not-prepared", str(job.error))
    job_settings = job.model_dump(mode="json").get("settings")
    if not isinstance(job_settings, dict):
        raise LiveAdapterError(
            "render-job-settings-missing",
            f"prepare_render_job returned no settings readback: {job.error}",
        )
    mismatches = sorted(key for key, value in ordered.items() if job_settings.get(key) != value)
    if mismatches:
        raise LiveAdapterError(
            "render-job-settings-mismatch",
            f"queued job settings differ at {mismatches}: "
            f"{[(k, job_settings.get(k), ordered[k]) for k in mismatches]}",
        )
    job_id = str(job.job_id)
    started_wall = time.time()
    outcome = McpActionOutcome.model_validate(
        ctx.transport("render", "start", {"job_ids": [job_id]})
    )
    if not outcome.ok:
        raise LiveAdapterError("render-start-failed", str(outcome.error))

    wait_for_completion(ctx, job_id)

    candidates = [
        hit
        for hit in sorted(target.glob(f"{p.custom_name}.mp4"))
        if hit.stat().st_mtime >= started_wall and hit.stat().st_size > 0
    ]
    if not candidates:
        raise LiveAdapterError(
            "render-output-missing", f"job {job_id} produced no fresh file"
        )
    if len(candidates) > 1:
        names = [c.name for c in candidates]
        raise LiveAdapterError("render-output-ambiguous", f"multiple outputs: {names}")
    output = candidates[0]

    probe = validate_native_media(
        output,
        load_ffprobe(),
        expected_width=p.width,
        expected_height=p.height,
        expected_fps=p.frame_rate,
        expected_video_codec=p.codec_id,
        expected_audio_codec="aac",
        expected_channels=2,
        expected_sample_rate=48000,
    )
    sha = sha256_file(output)

    deleted = McpActionOutcome.model_validate(
        ctx.transport("render", "delete_job", {"job_id": job_id})
    )
    if not deleted.ok:
        raise LiveAdapterError("render-delete-failed", str(deleted.error))
    return persist_meta(
        ctx, target, p, job_id=job_id, output=output, sha=sha, probe=probe, reused=False
    )


__all__ = ["render_native"]
