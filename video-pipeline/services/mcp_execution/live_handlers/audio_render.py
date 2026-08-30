"""Resolve-native probe render for the Task 5 audio stages.

One bounded render through the pinned MCP (``render.prepare_render_job`` →
``start`` → ``wait_for_completion``), with the Task 4-measured freshness
discipline: matching stale outputs are deleted up front, completion is the
polled job's own ``CompletionPercentage == 100`` with a not-rendering
witness, and only files whose mtime postdates the job start count. The
boundary report is never a substitute for the rendered media.

The poll itself is the Task 7 lifecycle (``render_poll``): progress-
adaptive deadline (a real-episode render outlasts any fixed probe-scale
budget) and stop + delete of the exact job on timeout, so a failed probe
can never leave an orphaned render blocking the queue (measured on
v44-real-01: the loudness poll's fixed 240s budget orphaned a job that
kept rendering past the failed run). A completed probe render deletes
its finished job too — the queue must not accumulate one entry per
attempt (measured on v44-real-01: three completed loudness jobs left
queued after a single run).
"""

from __future__ import annotations

import time
from pathlib import Path

from services.mcp_client.ops_models import AddJobResult, McpActionOutcome
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveSessionContext,
    require_ok,
)
from services.mcp_execution.live_handlers.render_poll import (
    RENDER_OPERATION_TIMEOUT_SECONDS,
    wait_for_completion,
)


def render_fresh_audio(ctx: LiveSessionContext, name: str) -> Path:
    """Render the current timeline once; return the fresh output path."""

    if ctx.render_dir is None:
        raise LiveAdapterError(
            "render-target-unconfigured",
            "the audio stages need a configured render directory",
        )
    render_dir = Path(ctx.render_dir)
    render_dir.mkdir(parents=True, exist_ok=True)
    for stale in render_dir.glob(f"{name}*"):
        stale.unlink()
    job = AddJobResult.model_validate(
        ctx.transport(
            "render",
            "prepare_render_job",
            {
                "target_dir": str(render_dir),
                "custom_name": name,
                "format": "mp4",
                "codec": "h264",
                "require_temp_target": False,
                "settings": {"ExportVideo": True, "DataBurnIn": "None"},
            },
            timeout_seconds=RENDER_OPERATION_TIMEOUT_SECONDS,
        )
    )
    if not job.ok or not job.job_id:
        raise LiveAdapterError(
            "render-job-not-prepared", f"prepare_render_job refused: {job.error}"
        )
    started = time.time()
    require_ok(
        McpActionOutcome.model_validate(
            ctx.transport(
                "render",
                "start",
                {"job_ids": [job.job_id]},
                timeout_seconds=RENDER_OPERATION_TIMEOUT_SECONDS,
            )
        ),
        "render-start",
    )
    wait_for_completion(ctx, str(job.job_id))
    fresh = [
        hit
        for hit in sorted(render_dir.glob(f"{name}*"))
        if hit.stat().st_mtime >= started and hit.stat().st_size > 0
    ]
    if not fresh:
        raise LiveAdapterError(
            "render-output-missing", f"render {name} completed but produced no fresh file"
        )
    # Completed-job cleanup (Task 7 lifecycle): the probe seam must not
    # leave its finished job in the vendor queue — measured on v44-real-01,
    # a loudness run accumulated 3 completed jobs that persist past the run.
    deleted = McpActionOutcome.model_validate(
        ctx.transport(
            "render",
            "delete_job",
            {"job_id": str(job.job_id)},
            timeout_seconds=RENDER_OPERATION_TIMEOUT_SECONDS,
        )
    )
    if not deleted.ok:
        raise LiveAdapterError(
            "render-delete-failed",
            f"render {name}: completed job {job.job_id} delete refused: {deleted.error}",
        )
    return fresh[-1]


__all__ = ["render_fresh_audio"]
