"""One consultation sample render (wave 1, P2-1 split d).

Entry half of the moved render flow: the idempotent entry state
machine (P1-1) plus the stored-path completion (P1-3/P1-5). The owner
render + publish half lives in sample_publish.py. States: ``stored``
and ``recovering`` return WITHOUT entering the render function and
WITHOUT budget writes; ``in_progress`` answers a parallel caller that
lost the owner lock with zero work and zero writes; only the owner of
the open attempt renders.
"""

from __future__ import annotations

import fcntl
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from services.cli.preview_render import render_consultation_sample
from services.cli.review_common import load_tools
from services.cli.sample_owned import OwnerRun, RenderDeps, _render_owned
from services.cli.sample_publish import _presentation_notes
from services.cli.sample_resolve import (
    SampleRenderFn,
    SampleResolveFn,
    default_sample_context,
    pre_render_inputs_ready,
)
from services.episode_cockpit.consultation_selection_budget import (
    BudgetResult,
    settle_preview,
)
from services.episode_cockpit.consultation_store import consultation_write_locked
from services.episode_cockpit.errors import (
    CockpitConflictError,
)
from services.episode_cockpit.sample_complete import (
    complete_missing_budget_settle_locked,
    verify_recovery,
)
from services.episode_cockpit.sample_identity import (
    SampleManifestV1,
    SampleRequestIdentityV1,
    derive_sample_id,
    episode_render_lock_path,
    render_lock_path,
    sample_identity_digest,
    verify_sample_content,
)
from services.episode_cockpit.sample_journal import (
    complete_missing_success_locked,
    request_sample_locked,
    settle_sample_failed,
    settle_sample_failed_locked,
)

if TYPE_CHECKING:
    from services.cli.episode_runner_rebuild import RebuildStageError
    from services.contracts.timeline_ir import TimelineIr0C
    from services.preview.models import PresentationRenderSettings, TracePresentation

__all__ = ["Claim", "EntryContext", "default_sample_render", "render_sample_now"]


def default_sample_render(  # noqa: PLR0913 (render-seam signature mirrors the real adapter)
    sample_ir: TimelineIr0C,
    mezzanine: Path,
    out_dir: Path,
    *,
    timeout_seconds: float | None,
    presentation: PresentationRenderSettings | None,
    presentation_trace: TracePresentation | None,
) -> None:
    render_consultation_sample(
        sample_ir, mezzanine, out_dir, tools=load_tools(),
        timeout_seconds=timeout_seconds, presentation=presentation,
        presentation_trace=presentation_trace,
    )


def _try_render_lock(episode_root: Path, sample_id: str) -> BinaryIO | None:
    """Non-blocking per-sample owner lock; None when a parallel render holds it."""
    path = render_lock_path(episode_root, sample_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        return None
    return stream


def _acquire_episode_render_lock(episode_root: Path) -> BinaryIO:
    """Blocking episode-wide render lock: sample renders serialize.

    Only owners take it (second callers return ``in_progress`` on the
    per-sample miss above and never wait here), and it is held across
    reserve + render + settle — so the next serialized render always
    sees the settled remainder. The file persists (never unlinked):
    exclusion rests on one stable inode.
    """
    path = episode_render_lock_path(episode_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    return stream


@dataclass(frozen=True, slots=True)
class Claim:
    """Joined-request outcome: what the entry lock section decided."""

    request_state: str
    manifest: SampleManifestV1
    open_attempt: str | None
    owner_lock: BinaryIO | None


@dataclass(frozen=True, slots=True)
class EntryContext:
    """Everything the post-entry paths need from the joined request."""

    episode_root: Path
    identity: SampleRequestIdentityV1
    sample_id: str
    digest: str
    claim: Claim


def render_sample_now(
    episode_root: Path,
    identity: SampleRequestIdentityV1,
    *,
    sample_attempt_id: str,
    render_fn: SampleRenderFn | None = None,
    resolve_fn: SampleResolveFn | None = None,
) -> dict[str, Any]:
    """Render one sample as the owner of its open reserve (P1-1/P1-2/P1-3/P1-5)."""
    from services.cli.episode_runner_rebuild import (  # noqa: PLC0415 (lazy: rebuild owns the error type)
        RebuildStageError,
    )

    resolve = resolve_fn or default_sample_context
    render = render_fn or default_sample_render
    sample_id = derive_sample_id(identity)
    digest = sample_identity_digest(identity)
    run = OwnerRun()

    def _settle_budget(result: BudgetResult, failure_code: str | None) -> None:
        if run.attempt is None or run.budget_settled:
            return
        run.budget_settled = True
        wall_elapsed = (
            run.wall_override
            if run.wall_override is not None
            else max(time.monotonic() - run.render_started, 0.001)
        )
        settle_preview(
            episode_root, run.attempt, preview_seconds=run.preview_seconds,
            wall_elapsed=wall_elapsed, result=result, failure_code=failure_code,
        )

    def _fail(
        code: str, detail: str, *, locked: bool, conflict: bool = False
    ) -> RebuildStageError | CockpitConflictError:
        _settle_budget("failed", code)
        message = f"{code}: {detail}"
        if locked:
            settle_sample_failed_locked(
                episode_root, identity, message, sample_attempt_id=claim.open_attempt)
        else:
            settle_sample_failed(
                episode_root, identity, message, sample_attempt_id=claim.open_attempt)
        if conflict:
            return CockpitConflictError(code, detail)
        return RebuildStageError(code, detail)

    if identity.output_id != "landscape":
        _settle_budget("failed", "sample-output-unsupported")
        settle_sample_failed(
            episode_root, identity,
            "sample-output-unsupported: 試し動画は landscape のみ対応しています。",
            sample_attempt_id=sample_attempt_id,
        )
        raise RebuildStageError(
            "sample-output-unsupported",
            "試し動画は landscape のみ対応しています。"
            f"要求された出力 ({identity.output_id}) には対応していません。",
        )
    if resolve_fn is None and not pre_render_inputs_ready(episode_root):
        # Fail fast BEFORE the blocking episode render lock: inputs that can
        # never resolve must be a visible typed 4xx, never an infinite wait.
        _settle_budget("failed", "sample-bundle-unreadable")
        settle_sample_failed(
            episode_root, identity,
            "sample-bundle-unreadable: 編集記録が読めず、試し動画を作れませんでした。",
            sample_attempt_id=sample_attempt_id,
        )
        raise RebuildStageError(
            "sample-bundle-unreadable",
            "編集記録が読めず、試し動画を作れませんでした。",
        )
    claim = _join_request(episode_root, identity, sample_id, sample_attempt_id)
    entry = EntryContext(episode_root, identity, sample_id, digest, claim)
    if claim.request_state in ("stored", "recovering"):
        return _finish_stored(entry, resolve)
    if claim.owner_lock is None:
        return {
            "state": "in_progress",
            "manifest": claim.manifest,
            "sample_attempt_id": claim.open_attempt,
        }
    episode_lock = _acquire_episode_render_lock(episode_root)
    try:
        return _render_owned(entry, run, RenderDeps(resolve, render, _fail, _settle_budget))
    finally:
        episode_lock.close()
        claim.owner_lock.close()
        with suppress(OSError):
            render_lock_path(episode_root, sample_id).unlink()


def _join_request(
    episode_root: Path,
    identity: SampleRequestIdentityV1,
    sample_id: str,
    sample_attempt_id: str,
) -> Claim:
    """Entry lock section: join the journal, verify ownership, take the owner lock."""
    from services.cli.episode_runner_rebuild import (  # noqa: PLC0415 (lazy: rebuild owns the error type)
        RebuildStageError,
    )

    with consultation_write_locked(episode_root):
        outcome = request_sample_locked(episode_root, identity)
        if outcome["state"] in ("stored", "recovering"):
            return Claim(outcome["state"], outcome["manifest"], None, None)
        open_attempt: str | None = outcome["sample_attempt_id"]
        if open_attempt != sample_attempt_id:
            raise RebuildStageError(
                "sample-attempt-not-open",
                "試し動画の試行記録が開いていないため、描画を始めませんでした。"
                "要求を作り直してください。",
            )
        return Claim("reserved", outcome["manifest"], open_attempt,
                     _try_render_lock(episode_root, sample_id))


def _finish_stored(entry: EntryContext, resolve: SampleResolveFn) -> dict[str, Any]:
    """Stored path: re-verify (zero render, zero budget) + complete missing journals."""
    from services.cli.episode_runner_rebuild import (  # noqa: PLC0415 (lazy: rebuild owns the error type)
        RebuildStageError,
    )

    episode_root, identity = entry.episode_root, entry.identity
    sample_id, digest, claim = entry.sample_id, entry.digest, entry.claim
    try:
        ctx = resolve(episode_root, identity)
    except Exception as error:
        raise RebuildStageError("sample-render-failed", str(error)) from error
    with consultation_write_locked(episode_root):
        manifest = verify_recovery(episode_root, sample_id, identity)
        verify_sample_content(ctx.sample_ir, manifest)
        success_done = complete_missing_success_locked(
            episode_root, sample_id, digest, identity, manifest.sample_attempt_id)
        settle_done = complete_missing_budget_settle_locked(
            episode_root, manifest, identity)
        state = (
            "stored"
            if claim.request_state == "stored" and not success_done and not settle_done
            else "recovering"
        )
        return {
            "state": state, "manifest": manifest,
            "presentation_notes": _presentation_notes(ctx.presentation),
        }
