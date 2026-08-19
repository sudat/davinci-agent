"""Deterministic Final Render job lifecycle: create/start/monitor/cancel.

Completion is detected ONLY via the frozen ``CompletionPercentage == 100``
rule (localized job-status strings are never parsed as truth). The single
place a status string IS read is the false-complete trap: a status claiming
completion (live-verified localized samples: ``Complete`` / ``完了``) while
the percentage is below 100 — or absent — is a lying job and fails
permanently without retry. Deadline timeouts and bridge connection drops
are the only transient codes and retry under a bounded attempt budget; every
other failure is permanent. Outputs may land only under the declared
allowlist — any other target is a typed refusal BEFORE rendering starts.
"""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from services.build.builder_models import RenderTiming
from services.build.render_models import (
    OutputAllowlist,
    RenderCancel,
    RenderJobAttempt,
    RenderJobFailure,
    RenderJobRecord,
    RenderJobRequest,
    preset_sha256,
)
from services.resolve_bridge.connection import BridgeConnectionError
from services.toolchain.render_qc import render_complete

if TYPE_CHECKING:
    from services.build.render_models import RenderProjectApi

COMPLETE_LIKE_STATUS: Final[frozenset[str]] = frozenset({"Complete", "完了"})
DEFAULT_RENDER_ATTEMPTS: Final = 2


class RenderJobRunner:
    """Runs one deterministic render job with a bounded transient-retry budget."""

    def __init__(
        self,
        project: RenderProjectApi,
        allowlist: OutputAllowlist,
        *,
        timing: RenderTiming | None = None,
        max_attempts: int = DEFAULT_RENDER_ATTEMPTS,
        cancel: RenderCancel | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._project = project
        self._allowlist = allowlist
        self._timing = timing if timing is not None else RenderTiming()
        self._max_attempts = max_attempts
        self._cancel = cancel if cancel is not None else RenderCancel()

    def cancel(self, detail: str = "cancelled by operator") -> None:
        """Explicit cancel: stop rendering and fail the monitor typed."""

        self._cancel.requested = True
        self._cancel.detail = detail
        self._stop()

    def run(self, request: RenderJobRequest) -> RenderJobRecord:
        rows: list[RenderJobAttempt] = []
        for index in range(1, self._max_attempts + 1):
            try:
                return self._attempt(request, index, tuple(rows))
            except RenderJobFailure as failure:
                if failure.attempt_rows:
                    rows = list(failure.attempt_rows)
                if failure.failure_class == "transient" and index < self._max_attempts:
                    continue
                raise RenderJobFailure(
                    failure.code,
                    failure.detail,
                    attempts=index,
                    attempt_rows=tuple(rows),
                ) from failure
        raise RenderJobFailure("render-job-failed", "render attempt budget exhausted")

    def _attempt(
        self, request: RenderJobRequest, index: int, prior: tuple[RenderJobAttempt, ...]
    ) -> RenderJobRecord:
        preset = request.preset
        if not self._project.SetCurrentRenderFormatAndCodec(
            preset.video_format, preset.video_codec
        ):
            raise RenderJobFailure(
                "render-setup-failed",
                f"SetCurrentRenderFormatAndCodec({preset.video_format},"
                f"{preset.video_codec}) failed",
            )
        settings: dict[str, object] = {
            "TargetDir": request.render_dir,
            "CustomName": request.custom_name,
            "FormatWidth": preset.width,
            "FormatHeight": preset.height,
            "FrameRate": preset.frame_rate.num,
            "AudioCodec": preset.audio_codec,
            "AudioSampleRate": preset.audio_sample_rate,
            "SelectAllFrames": True,
        }
        if not self._project.SetRenderSettings(settings):
            raise RenderJobFailure("render-setup-failed", "SetRenderSettings failed")
        job_id = self._project.AddRenderJob()
        if not isinstance(job_id, str) or not job_id:
            raise RenderJobFailure("render-job-failed", "AddRenderJob returned no job id")
        entry = _job_entry(self._project, job_id)
        target_dir = entry.get("TargetDir")
        filename = entry.get("OutputFilename")
        if not isinstance(target_dir, str) or not isinstance(filename, str) or not filename:
            raise RenderJobFailure(
                "render-job-failed",
                f"render job {job_id} entry lacks an output location: {entry!r}",
            )
        output = Path(target_dir) / filename
        if not self._allowlist.permits(output):
            raise RenderJobFailure(
                "output-path-not-allowlisted", self._allowlist.refusal_detail(output)
            )
        if not self._project.StartRendering(job_id):
            raise RenderJobFailure("render-start-failed", f"StartRendering failed for {job_id}")
        context = _MonitorContext(
            request=request, index=index, prior=prior, job_id=job_id, output=output
        )
        return self._monitor(context)

    def _monitor(self, context: _MonitorContext) -> RenderJobRecord:
        percentage_seen = -1
        polls = 0
        deadline = time.monotonic() + self._timing.deadline_seconds
        while time.monotonic() < deadline:
            if self._cancel.requested:
                self._stop()
                raise RenderJobFailure(
                    "render-cancelled",
                    f"render job {context.job_id} cancelled: {self._cancel.detail}",
                )
            try:
                status = self._project.GetRenderJobStatus(context.job_id)
            except BridgeConnectionError as error:
                self._stop()
                raise RenderJobFailure(
                    "resolve_disconnect",
                    f"render status poll lost the bridge for {context.job_id}: {error}",
                    attempt_rows=_rows(
                        context, "resolve_disconnect", percentage_seen, polls, str(error)
                    ),
                ) from error
            polls += 1
            percentage = status.get("CompletionPercentage")
            if isinstance(percentage, int) and not isinstance(percentage, bool):
                percentage_seen = percentage
            if render_complete(status):
                return self._complete(context, percentage_seen, polls)
            job_status = status.get("JobStatus")
            if isinstance(job_status, str) and job_status in COMPLETE_LIKE_STATUS:
                self._stop()
                raise RenderJobFailure(
                    "render-false-complete",
                    f"render job {context.job_id} status {job_status!r} claims completion at "
                    f"CompletionPercentage {percentage_seen} (<100); status strings "
                    "are never truth",
                )
            self._timing.sleep(self._timing.poll_seconds)
        self._stop()
        detail = (
            f"render job {context.job_id} did not reach CompletionPercentage==100 within "
            f"{self._timing.deadline_seconds:.0f}s "
            f"(last {percentage_seen} after {polls} polls)"
        )
        raise RenderJobFailure(
            "timeout",
            detail,
            attempt_rows=_rows(context, "timeout", percentage_seen, polls, detail),
        )

    def _complete(self, context: _MonitorContext, percentage: int, polls: int) -> RenderJobRecord:
        if not context.output.is_file():
            raise RenderJobFailure(
                "render-output-missing", f"completed render has no output: {context.output}"
            )
        rows = _rows(context, "complete", percentage, polls, "")
        request = context.request
        return RenderJobRecord(
            preset_sha256=preset_sha256(request.preset),
            timeline_conformance_fingerprint=request.timeline_conformance_fingerprint,
            output_path=str(context.output),
            job_id=context.job_id,
            attempts=rows,
            poll_count_total=sum(row.poll_count for row in rows),
        )

    def _stop(self) -> None:
        with suppress(BridgeConnectionError):
            self._project.StopRendering()


AttemptOutcome = Literal[
    "complete", "timeout", "resolve_disconnect", "false_complete", "cancelled"
]


@dataclass(frozen=True, slots=True)
class _MonitorContext:
    request: RenderJobRequest
    index: int
    prior: tuple[RenderJobAttempt, ...]
    job_id: str
    output: Path


def _rows(
    context: _MonitorContext,
    outcome: AttemptOutcome,
    percentage: int,
    polls: int,
    detail: str,
) -> tuple[RenderJobAttempt, ...]:
    row = RenderJobAttempt(
        attempt=context.index,
        job_id=context.job_id,
        outcome=outcome,
        completion_percentage=percentage,
        poll_count=polls,
        detail=detail,
    )
    return (*context.prior, row)


def _job_entry(project: RenderProjectApi, job_id: str) -> dict[str, object]:
    for entry in project.GetRenderJobList():
        if entry.get("JobId") == job_id:
            return entry
    raise RenderJobFailure("render-job-failed", f"render job {job_id} missing from job list")


__all__ = [
    "COMPLETE_LIKE_STATUS",
    "DEFAULT_RENDER_ATTEMPTS",
    "RenderJobRunner",
]
