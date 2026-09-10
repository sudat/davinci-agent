"""Sample owner render (wave 1, P2-1 split e).

Owner half of the render entry: resolve, atomic wall+preview budget
claim (P1-2), the render call, temp manifest staging, then handoff to
sample_publish. Imported by sample_render; imports sample_publish for
the final handoff (one direction only).
"""

from __future__ import annotations

import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.cli.sample_publish import PublishHandoff, _publish_temp
from services.cli.sample_resolve import next_sample_sequence
from services.episode_cockpit.consultation_selection_budget import (
    BudgetResult,
    SelectionAttempt,
    SelectionBudgetEntryV1,
    append_selection_budget_entry,
    attempt_for,
)
from services.episode_cockpit.consultation_store import (
    combined_wall_used_in_scope,
    consultation_write_locked,
    ensure_preview_budget_available,
    load_budget_limits,
)
from services.episode_cockpit.errors import (
    CockpitError,
    CockpitNotFoundError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.sample_identity import (
    SAMPLE_PREVIEW_NAME,
    SampleRequestIdentityV1,
    samples_root,
)
from services.foundation_io import sha256_file

if TYPE_CHECKING:
    from services.cli.sample_render import EntryContext
    from services.cli.sample_resolve import SampleRenderFn, SampleResolveFn

type FailFn = Callable[..., Exception]
type SettleFn = Callable[[BudgetResult, str | None], None]


@dataclass
class OwnerRun:
    """Mutable owner progress: budget claim + render clock."""

    attempt: SelectionAttempt | None = None
    preview_seconds: float = 0.0
    budget_settled: bool = False
    render_started: float = 0.0
    wall_override: float | None = None
    wall_reserved: float = 0.0


def _clamp_render_timeout(requested_wall: float, reserved_wall: float) -> float:
    """A render may never run longer than its own reservation.

    The ledger holds ``reserved_wall`` for this attempt, so the renderer
    timeout is clamped to it — the measured total stays within what the
    reservation secured.
    """
    return min(requested_wall, reserved_wall)


@dataclass(frozen=True, slots=True)
class RenderDeps:
    """Injectable seams + settle callbacks for the owner path."""

    resolve: SampleResolveFn
    render: SampleRenderFn
    fail: FailFn
    settle: SettleFn


def _wall_reserve_for(remaining_wall: float) -> float:
    """The wall allowance one sample attempt reserves.

    Single definition of the reserve amount: the ledger line below and
    the renderer timeout (via ``OwnerRun.wall_reserved`` +
    ``_clamp_render_timeout``) both derive from this, so the reserve
    amount and the execution cap are THE SAME value by construction.
    Sample renders serialize per episode, so reserving the FULL
    remaining wall is safe: the open reservation folds into the wall
    and the next serialized render sees the reduced remainder, keeping
    the combined measured total within the consultation cap.
    """
    return remaining_wall


def _atomic_sample_reserve_locked(
    episode_root: Path,
    identity: SampleRequestIdentityV1,
    preview_seconds: float,
    remaining_wall: float,
) -> SelectionAttempt:
    """One ledger line securing wall + preview seconds together (P1-2)."""
    attempt = attempt_for(
        next_sample_sequence(episode_root),
        identity.consultation_id,
        identity.judgment_id,
    )
    append_selection_budget_entry(
        episode_root,
        SelectionBudgetEntryV1(
            attempt_id=attempt.attempt_id,
            consultation_id=attempt.consultation_id,
            judgment_id=attempt.judgment_id,
            reservation_sequence=attempt.reservation_sequence,
            phase="preview_reserved",
            llm_calls_reserved=0,
            llm_calls_used=0,
            wall_seconds_reserved=_wall_reserve_for(remaining_wall),
            wall_seconds_used=0.0,
            preview_seconds_reserved=preview_seconds,
            preview_seconds_used=0.0,
            scope="sample",
            created_at=datetime.now(UTC).isoformat(),
        ),
    )
    return attempt


def _render_owned(entry: EntryContext, run: OwnerRun, deps: RenderDeps) -> dict[str, Any]:
    """Owner path: resolve, atomic reserve, render, stage the manifest, publish."""
    from services.cli.episode_runner_rebuild import (  # noqa: PLC0415 (lazy: rebuild owns the error type)
        RebuildStageError,
    )
    from services.episode_cockpit.sample_complete import (  # noqa: PLC0415 (terminal manifest at use site)
        write_published_manifest,
    )

    episode_root, identity = entry.episode_root, entry.identity
    sample_id, digest = entry.sample_id, entry.digest
    resolve, render, _fail = deps.resolve, deps.render, deps.fail
    try:
        ctx = resolve(episode_root, identity)
    except (RebuildStageError, CockpitError) as error:
        raise _fail(error.code, error.detail, locked=False) from error
    except Exception as error:
        raise _fail("sample-render-failed", str(error), locked=False) from error
    run.preview_seconds = ctx.total_seconds
    remaining_wall = _reserve_owned(episode_root, identity, run, _fail)
    temp_dir = samples_root(episode_root) / f".tmp-{sample_id}-{uuid.uuid4().hex[:8]}"
    shutil.rmtree(temp_dir, ignore_errors=True)
    try:
        render(
            ctx.sample_ir, ctx.mezzanine, temp_dir,
            timeout_seconds=_clamp_render_timeout(remaining_wall, run.wall_reserved),
            presentation=(
                ctx.presentation.to_render_settings()
                if ctx.presentation is not None else None),
            presentation_trace=(
                ctx.presentation.to_trace_presentation()
                if ctx.presentation is not None else None),
        )
    except (RebuildStageError, CockpitError) as error:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise _fail(error.code, error.detail, locked=False) from error
    except Exception as error:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise _fail("sample-render-failed", str(error), locked=False) from error
    try:
        content_sha = sha256_file(temp_dir / SAMPLE_PREVIEW_NAME)
    except OSError as error:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise _fail(
            "sample-render-failed",
            "試し動画の描画結果が残らなかったため、確定していません。",
            locked=False,
        ) from error
    wall_measured = max(time.monotonic() - run.render_started, 0.001)
    run.wall_override = wall_measured
    if run.attempt is None:
        raise _fail(
            "sample-render-failed",
            "予算の確保が残らなかったため、確定していません。",
            locked=False,
        )
    try:
        published = write_published_manifest(
            temp_dir, identity, sample_ir=ctx.sample_ir,
            total_seconds=run.preview_seconds, content_sha256=content_sha,
            sample_attempt_id=entry.claim.open_attempt or "",
            budget_entry_id=run.attempt.attempt_id,
            budget_reservation_sequence=run.attempt.reservation_sequence,
            run_id=uuid.uuid4().hex[:12], wall_seconds_used=wall_measured,
        )
    except Exception as error:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise _fail("sample-render-failed", str(error), locked=False) from error
    return _publish_temp(
        PublishHandoff(episode_root, identity, sample_id, digest, ctx,
                       temp_dir, published, entry.claim.open_attempt or ""),
        deps.fail, deps.settle, resolve,
    )


def _reserve_owned(
    episode_root: Path,
    identity: SampleRequestIdentityV1,
    run: OwnerRun,
    _fail: FailFn,
) -> float:
    """Budget gates + atomic wall/preview reserve under the write lock.

    The reserve amount and the renderer timeout cap are THE SAME value:
    the attempt reserves the full remaining wall and the render runs
    with exactly that as its timeout, so a serialized render's measured
    total can never exceed what the ledger holds for it. Starting needs
    a positive remainder — an exhausted wall refuses fail-fast with a
    typed stop instead of rendering past the consultation cap.
    """
    with consultation_write_locked(episode_root):
        try:
            ensure_preview_budget_available(
                episode_root, load_budget_limits(), run.preview_seconds)
        except (CockpitUnprocessableError, CockpitNotFoundError) as error:
            raise _fail(error.code, error.detail, locked=True) from error
        remaining_wall = load_budget_limits().wall_seconds_limit - (
            combined_wall_used_in_scope(episode_root, "sample", "full_rebuild_exempt"))
        if remaining_wall <= 0:
            raise _fail(
                "consultation-selection-deadline-exceeded",
                "この相談の処理時間上限に達したため、試し動画を作りませんでした。",
                locked=True,
            ) from None
        run.attempt = _atomic_sample_reserve_locked(
            episode_root, identity, run.preview_seconds, remaining_wall)
        run.wall_reserved = _wall_reserve_for(remaining_wall)
        run.render_started = time.monotonic()
        return remaining_wall
