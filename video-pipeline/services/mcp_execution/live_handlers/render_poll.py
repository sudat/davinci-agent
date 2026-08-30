"""Render-state preflight and exact-job completion polling (Task 7).

Completion contract: poll the EXACT job id; require
``CompletionPercentage == 100`` AND a not-rendering witness. The per-job
``IsRenderingInProgress`` value is preferred when the build reports it;
when absent (measured on Resolve 21.0.4.5) the pinned MCP's global
``render.is_rendering`` boolean is read in the SAME poll cycle — valid
because the preflight proved the queue stopped and this single-writer
product starts exactly one job. The vendor's PascalCase ``Error`` payload
fails the step immediately; localized ``JobStatus`` prose is never a
signal; timeout stays typed.

Real-episode deadline discipline (measured on v44-real-01): a fixed
probe-scale budget kills healthy multi-minute renders and — worse —
leaves them RUNNING in Resolve, poisoning every later render-lifecycle
step. The deadline therefore tracks the job's own progress: each poll
that PROGRESSES (percentage up or ETA down) re-arms the budget using the
vendor's ``EstimatedTimeRemainingInMs`` (factor + margin, floor
``RENDER_TIMEOUT_SECONDS``, hard ceiling). A job whose signals freeze is
a stall and expires. On expiry the exact job is stopped and deleted via
the pinned vendor actions so no orphan keeps the queue busy.
"""

from __future__ import annotations

import time

from pydantic import ValidationError

from services.mcp_client.errors import McpTimeoutError
from services.mcp_client.ops_models import (
    McpActionOutcome,
    RenderInProgressResult,
    RenderJobList,
    RenderJobStatus,
)
from services.mcp_execution.live_handlers.common import (
    LiveAdapterError,
    LiveSessionContext,
)

RENDER_TIMEOUT_SECONDS: float = 240.0
RENDER_POLL_SECONDS: float = 4.0
#: Hard wall for progress-chasing: no render may outlive this budget even
#: while its signals keep improving (episode-scale bound).
RENDER_TIMEOUT_CEILING_SECONDS: float = 7200.0
#: Progress re-arm: deadline = now + max(floor, eta_s * factor + margin).
RENDER_ETA_FACTOR: float = 2.0
RENDER_ETA_MARGIN_SECONDS: float = 90.0
#: Per-vendor-call deadline for every render lifecycle operation (rerun5
#: measured a get_job_status response exceeding the 10 s transport default
#: mid-render while the job progressed healthily). Same order as the
#: voice-isolation pair's measured bound.
RENDER_OPERATION_TIMEOUT_SECONDS: float = 120.0
_COMPLETE_PERCENT: float = 100.0


def global_rendering(ctx: LiveSessionContext) -> bool:
    """The pinned MCP's global render-queue state, strictly typed.

    Accepts either measured key shape (compound ``rendering`` / granular
    ``is_rendering``); missing, non-boolean, or contradictory shapes fail
    typed instead of being guessed.
    """

    raw = ctx.transport(
        "render", "is_rendering", {}, timeout_seconds=RENDER_OPERATION_TIMEOUT_SECONDS
    )
    try:
        result = RenderInProgressResult.model_validate(raw)
    except ValidationError as exc:
        raise LiveAdapterError(
            "render-global-state-malformed",
            f"render.is_rendering returned a non-boolean state {raw!r}: {exc}",
        ) from exc
    if result.rendering is not None and result.is_rendering is not None:
        if result.rendering != result.is_rendering:
            raise LiveAdapterError(
                "render-global-state-ambiguous",
                f"is_rendering aliases disagree: {raw!r}",
            )
        return result.rendering
    if result.rendering is not None:
        return result.rendering
    if result.is_rendering is not None:
        return result.is_rendering
    raise LiveAdapterError(
        "render-global-state-missing",
        f"render.is_rendering returned no boolean state: {raw!r}",
    )


def _listed_job_ids(ctx: LiveSessionContext) -> tuple[str, ...]:
    """Read-only job ids from the queue listing (best effort, typed)."""

    try:
        listing = RenderJobList.model_validate(
            ctx.transport(
                "render",
                "list_jobs",
                {},
                timeout_seconds=RENDER_OPERATION_TIMEOUT_SECONDS,
            )
        )
    except Exception as exc:
        raise LiveAdapterError("render-list-malformed", str(exc)) from exc
    if not isinstance(listing.jobs, tuple):
        raise LiveAdapterError("render-list-malformed", f"jobs not a tuple: {listing.jobs!r}")
    ids: list[str] = []
    for job in listing.jobs:
        if not isinstance(job, dict):
            raise LiveAdapterError("render-list-malformed", f"job not a dict: {job!r}")
        jid = job.get("job_id") or job.get("JobId") or job.get("id")
        if isinstance(jid, str) and jid:
            ids.append(jid)
    return tuple(ids)


def assert_render_stopped(ctx: LiveSessionContext) -> None:
    """Prove nothing is rendering BEFORE queuing or reusing.

    The pinned MCP global render-state action is called UNCONDITIONALLY —
    an empty ``list_jobs`` response alone is not proof that rendering has
    stopped. Listed jobs then refine the verdict with their per-job flags.
    Refusals name the blocking job ids (the precise measured blocker).
    """

    if global_rendering(ctx):
        ids = _listed_job_ids(ctx)
        named = f" (listed jobs: {', '.join(ids)})" if ids else " (no jobs listed)"
        raise LiveAdapterError(
            "render-already-in-progress",
            f"the pinned MCP reports a render globally in progress{named}",
        )
    for jid in _listed_job_ids(ctx):
        raw = ctx.transport(
            "render",
            "get_job_status",
            {"job_id": jid},
            timeout_seconds=RENDER_OPERATION_TIMEOUT_SECONDS,
        )
        try:
            st = RenderJobStatus.model_validate(raw)
        except Exception as exc:
            raise LiveAdapterError("render-status-malformed", str(exc)) from exc
        if st.is_rendering_in_progress is True:
            raise LiveAdapterError(
                "render-already-in-progress", f"job {jid} still rendering"
            )


def not_rendering_witness(ctx: LiveSessionContext, status: RenderJobStatus) -> bool:
    """Per-job flag when present; otherwise the same-cycle global boolean."""

    if status.is_rendering_in_progress is not None:
        return status.is_rendering_in_progress is False
    return not global_rendering(ctx)


def _stop_timed_out_job(ctx: LiveSessionContext, job_id: str) -> str:
    """Stop + delete OUR expired job so it cannot keep the queue busy.

    The vendor stop action is global; under the enforced single-job
    precondition (queue proven stopped before queuing, exactly one job
    started) it addresses exactly this job. Cleanup is best-effort with
    the outcome reported in the returned summary — never a silent
    success, never masking the timeout itself.
    """

    summary = ""
    try:
        stopped = McpActionOutcome.model_validate(
            ctx.transport(
                "render",
                "stop",
                {},
                timeout_seconds=RENDER_OPERATION_TIMEOUT_SECONDS,
            )
        )
        summary += "job stopped" if stopped.ok else f"stop refused: {stopped.error}"
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask the timeout
        summary += f"stop failed: {type(exc).__name__}"
    try:
        deleted = McpActionOutcome.model_validate(
            ctx.transport(
                "render",
                "delete_job",
                {"job_id": job_id},
                timeout_seconds=RENDER_OPERATION_TIMEOUT_SECONDS,
            )
        )
        summary += ", job deleted" if deleted.ok else f", delete refused: {deleted.error}"
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask the timeout
        summary += f", delete failed: {type(exc).__name__}"
    return summary + "; operator queue cleanup may be required"


def wait_for_completion(ctx: LiveSessionContext, job_id: str) -> None:
    """Poll the exact job until 100% + not-rendering, progress-adaptive."""

    started = time.monotonic()
    ceiling = started + RENDER_TIMEOUT_CEILING_SECONDS
    deadline = started + RENDER_TIMEOUT_SECONDS
    last_percent: float | None = None
    last_eta_ms: float | None = None
    while time.monotonic() < deadline:
        try:
            raw = ctx.transport(
                "render",
                "get_job_status",
                {"job_id": job_id},
                timeout_seconds=RENDER_OPERATION_TIMEOUT_SECONDS,
            )
            status = RenderJobStatus.model_validate(raw)
            if status.error_message is not None:
                raise LiveAdapterError(
                    "render-job-failed", f"job {job_id} failed: {status.error_message}"
                )
            if status.completion_percentage is None:
                raise LiveAdapterError(
                    "render-status-malformed",
                    f"job {job_id} missing CompletionPercentage: {raw!r}",
                )
            if (
                status.completion_percentage == _COMPLETE_PERCENT
                and not_rendering_witness(ctx, status)
            ):
                return
        except McpTimeoutError as exc:
            # Cleanup FIRST: rerun5's escaped raw timeout orphaned the
            # healthy render, which then starved every retry on the server.
            cleanup = _stop_timed_out_job(ctx, job_id)
            raise LiveAdapterError(
                "render-poll-timeout",
                f"job {job_id} poll transport call exceeded "
                f"{RENDER_OPERATION_TIMEOUT_SECONDS}s; {cleanup}",
            ) from exc
        percent = status.completion_percentage
        eta_ms = status.estimated_time_remaining_ms
        progressed = percent > (last_percent if last_percent is not None else -1.0) or (
            eta_ms is not None
            and (last_eta_ms is None or eta_ms < last_eta_ms)
        )
        if progressed:
            if eta_ms is not None:
                budget = max(
                    RENDER_TIMEOUT_SECONDS,
                    eta_ms / 1000.0 * RENDER_ETA_FACTOR + RENDER_ETA_MARGIN_SECONDS,
                )
            else:
                budget = RENDER_TIMEOUT_SECONDS
            deadline = min(ceiling, time.monotonic() + budget)
            last_percent = percent
            last_eta_ms = eta_ms if eta_ms is not None else last_eta_ms
        time.sleep(RENDER_POLL_SECONDS)
    cleanup = _stop_timed_out_job(ctx, job_id)
    raise LiveAdapterError(
        "render-timeout",
        f"job {job_id} never reached exactly 100% + not-rendering "
        f"(last percent={last_percent}, last eta_ms={last_eta_ms}); "
        f"{cleanup}",
    )


__all__ = [
    "RENDER_ETA_FACTOR",
    "RENDER_ETA_MARGIN_SECONDS",
    "RENDER_OPERATION_TIMEOUT_SECONDS",
    "RENDER_POLL_SECONDS",
    "RENDER_TIMEOUT_CEILING_SECONDS",
    "RENDER_TIMEOUT_SECONDS",
    "assert_render_stopped",
    "global_rendering",
    "not_rendering_witness",
    "wait_for_completion",
]
