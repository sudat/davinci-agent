# noqa: INP001 (evidence tree is not an importable package by design)
"""Render freshness for Task 4 probe tooling: poll-or-fail plus selfcheck.

``render_once`` renders one timeline to a FRESH file or fails loudly:
unless the job itself reports completion before the deadline, it raises —
a deadline expiry never falls through to directory discovery. Discovery is
bound to this run through the triple gate: matching stale files deleted up
front, job completion required, and only files whose mtime postdates the
job start accepted. ``selfcheck`` proves the refusal paths without Resolve.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from evidence_io import RENDER_DIR
from mcp_call import RenderSession, must, raw

#: A render job counts as complete only at 100% with no render in progress.
_COMPLETE_PERCENT = 100.0

#: Selfcheck fakes age artifacts one hour back to simulate stale output.
_STALE_AGE_SECONDS = 3600.0


def render_once(  # noqa: PLR0913 (probe knob bundle kept flat for call-site clarity)
    session: RenderSession,
    name: str,
    tl: str,
    *,
    render_dir: Path = RENDER_DIR,
    timeout_seconds: float = 150.0,
    poll_seconds: float = 4.0,
) -> Path:
    must(raw(session, "timeline", "set_current", {"name": tl}), "set_current for render")
    render_dir.mkdir(parents=True, exist_ok=True)
    for stale in render_dir.glob(f"{name}*"):
        stale.unlink()
    job = must(
        raw(
            session,
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
        ),
        "prepare_render_job",
    )
    started_at = time.time()
    must(raw(session, "render", "start", {"job_ids": [job["job_id"]]}), "render start")
    completed = False
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = raw(session, "render", "get_job_status", {"job_id": job["job_id"]})
        if (
            float(status.get("CompletionPercentage") or 0) >= _COMPLETE_PERCENT
            and not status.get("IsRenderingInProgress")
        ):
            completed = True
            break
        time.sleep(poll_seconds)
    if not completed:
        raise RuntimeError(
            f"render {name} did not report 100%/not-rendering before its "
            f"{timeout_seconds}s deadline"
        )
    fresh = [
        hit for hit in sorted(render_dir.glob(f"{name}*")) if hit.stat().st_mtime >= started_at
    ]
    if not fresh:
        raise RuntimeError(f"render {name} completed but produced no fresh file")
    return fresh[-1]


class _FakeRenderClient:
    """Minimal stand-in for the MCP client's render actions (non-live)."""

    def __init__(
        self, *, complete_after: int, artifact: Path | None, artifact_stale: bool
    ) -> None:
        self._complete_after = complete_after
        self._artifact = artifact
        self._artifact_stale = artifact_stale
        self.polls = 0

    def _call_action_json(
        self, tool: str, action: str, _params: dict[str, object]
    ) -> dict[str, object]:
        if action == "set_current":
            return {"success": True}
        if action == "prepare_render_job":
            return {"success": True, "job_id": "job-selfcheck"}
        if action == "start":
            return {"success": True}
        if action == "get_job_status":
            self.polls += 1
            if self.polls >= self._complete_after and self._complete_after > 0:
                if self._artifact is not None:
                    self._artifact.write_bytes(b"artifact")
                    if self._artifact_stale:
                        old = time.time() - _STALE_AGE_SECONDS
                        os.utime(self._artifact, (old, old))
                return {
                    "CompletionPercentage": _COMPLETE_PERCENT,
                    "IsRenderingInProgress": False,
                }
            return {"CompletionPercentage": 41.0, "IsRenderingInProgress": True}
        raise RuntimeError(f"unexpected {tool}.{action}")


def _fake_session(
    *, complete_after: int, artifact: Path | None, artifact_stale: bool = False
) -> RenderSession:
    holder = SimpleNamespace()
    holder.client = _FakeRenderClient(
        complete_after=complete_after, artifact=artifact, artifact_stale=artifact_stale
    )
    return holder  # type: ignore[return-value]


def selfcheck() -> int:
    """Non-live proof that render_once refuses stale and timed-out renders.

    Cases: (1) a job that never reaches 100% raises at its deadline even
    when a stale file with the target name already exists; (2) a completed
    job with only a pre-existing stale output raises instead of returning
    it; (3) a file written mid-run with a pre-run mtime is rejected by the
    freshness filter; (4) a genuinely fresh output is returned.
    """
    with tempfile.TemporaryDirectory() as tmp:
        rnd = Path(tmp)
        checks: list[tuple[str, bool]] = []

        stale = rnd / "t4r-control.mp4"
        stale.write_bytes(b"stale")
        old = time.time() - _STALE_AGE_SECONDS
        os.utime(stale, (old, old))
        never = _fake_session(complete_after=0, artifact=None)
        try:
            render_once(
                never,
                "t4r-control",
                "tl",
                render_dir=rnd,
                timeout_seconds=0.4,
                poll_seconds=0.05,
            )
        except RuntimeError as exc:
            checks.append(("timeout raises", "deadline" in str(exc)))
        else:
            checks.append(("timeout raises", False))

        no_output = _fake_session(complete_after=2, artifact=None)
        try:
            render_once(
                no_output,
                "t4r-control",
                "tl",
                render_dir=rnd,
                timeout_seconds=5.0,
                poll_seconds=0.05,
            )
        except RuntimeError as exc:
            checks.append(("stale-only refuses", "fresh" in str(exc)))
        else:
            checks.append(("stale-only refuses", False))

        stale_mtime = _fake_session(
            complete_after=2, artifact=rnd / "t4r-control.mp4", artifact_stale=True
        )
        try:
            render_once(
                stale_mtime,
                "t4r-control",
                "tl",
                render_dir=rnd,
                timeout_seconds=5.0,
                poll_seconds=0.05,
            )
        except RuntimeError as exc:
            checks.append(("old-mtime artifact refuses", "fresh" in str(exc)))
        else:
            checks.append(("old-mtime artifact refuses", False))

        fresh = _fake_session(
            complete_after=2, artifact=rnd / "t4r-control.mp4", artifact_stale=False
        )
        produced = render_once(
            fresh,
            "t4r-control",
            "tl",
            render_dir=rnd,
            timeout_seconds=5.0,
            poll_seconds=0.05,
        )
        checks.append(("fresh output returned", produced == rnd / "t4r-control.mp4"))

        for label, ok in checks:
            print(f"[{'ok' if ok else 'FAIL'}] selfcheck: {label}", flush=True)
        return 0 if all(ok for _, ok in checks) else 1


__all__ = ["render_once", "selfcheck"]
