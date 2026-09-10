"""Sample publish (wave 1, P2-1 split f).

Terminal half of the owner render: the lock-guarded temp→final publish
(P1-3) with verified recovery of a raced final. Called by sample_owned;
imported by sample_render only for shared notes.
"""

from __future__ import annotations

import fcntl
import shutil
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.episode_cockpit.consultation_selection_budget import BudgetResult
from services.episode_cockpit.consultation_store import consultation_write_locked
from services.episode_cockpit.errors import (
    CockpitConflictError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.sample_complete import (
    complete_missing_budget_settle_locked,
    verify_recovery,
)
from services.episode_cockpit.sample_identity import (
    SampleManifestV1,
    SampleRequestIdentityV1,
    sample_dir,
    verify_sample_content,
)

if TYPE_CHECKING:
    from services.cli.sample_resolve import SampleRenderContext, SampleResolveFn
    from services.episode_cockpit.presentation_overrides import PresentationOverrideSet

type FailFn = Callable[..., Exception]
type SettleFn = Callable[[BudgetResult, str | None], None]

@dataclass(frozen=True, slots=True)
class PublishHandoff:
    """Everything the publish step needs after the render finished."""

    episode_root: Path
    identity: SampleRequestIdentityV1
    sample_id: str
    digest: str
    ctx: SampleRenderContext
    temp_dir: Path
    published: SampleManifestV1
    open_attempt: str


def _presentation_notes(
    presentation_set: PresentationOverrideSet | None,
) -> list[dict[str, Any]]:
    if presentation_set is None:
        return []
    return [note.model_dump(mode="json") for note in presentation_set.notes]

def _publish_temp(
    handoff: PublishHandoff, _fail: FailFn, _settle_budget: SettleFn,
    resolve: SampleResolveFn,
) -> dict[str, Any]:
    """Rename temp→final under both locks; recover a raced final instead."""
    from services.cli.episode_runner_rebuild import (  # noqa: PLC0415 (lazy: rebuild owns the error type)
        RebuildStageError,
    )
    from services.episode_cockpit.sample_journal import (  # noqa: PLC0415 (publish-time journal at use site)
        record_sample_success_locked,
    )

    episode_root, identity = handoff.episode_root, handoff.identity
    sample_id, digest, ctx = handoff.sample_id, handoff.digest, handoff.ctx
    temp_dir, published = handoff.temp_dir, handoff.published
    target_dir = sample_dir(episode_root, sample_id)
    runner_lock_path = episode_root / "runner.lock"
    runner_lock_path.parent.mkdir(parents=True, exist_ok=True)
    runner_lock = runner_lock_path.open("a+b")
    try:
        try:
            fcntl.flock(runner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise _fail(
                "runner-active",
                "他の編集処理が動いているため、試し動画を確定しませんでした。",
                locked=False,
            ) from error
        try:
            with consultation_write_locked(episode_root):
                try:
                    fresh = resolve(episode_root, identity)
                except Exception as error:
                    raise _fail(
                        "sample-render-failed", str(error), locked=True) from error
                if (
                    fresh.head_version != ctx.head_version
                    or fresh.plan_sha256 != ctx.plan_sha256
                    or fresh.policy_sha256 != ctx.policy_sha256
                    or fresh.full_ir_sha256 != ctx.full_ir_sha256
                ):
                    raise _fail(
                        "sample-base-changed",
                        "描画中に編集の版が変わりました。依頼を作り直してください。",
                        locked=True,
                    ) from None
                if target_dir.exists():
                    return _recover_raced_final(
                        handoff, fresh, _fail, _settle_budget, temp_dir)
                try:
                    temp_dir.rename(target_dir)
                except OSError as error:
                    raise _fail(
                        "sample-publish-conflict",
                        "他の試し動画の確定と重なったため、"
                        "試し動画を確定しませんでした。",
                        locked=True, conflict=True,
                    ) from error
                record_sample_success_locked(
                    episode_root, sample_id=sample_id, digest=digest,
                    operation_id=identity.operation_id,
                    sample_attempt_id=handoff.open_attempt,
                )
                _settle_budget("succeeded", None)
                return {
                    "state": "published", "manifest": published,
                    "presentation_notes": _presentation_notes(ctx.presentation),
                }
        except (RebuildStageError, CockpitConflictError):
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        finally:
            with suppress(OSError):
                fcntl.flock(runner_lock.fileno(), fcntl.LOCK_UN)
    finally:
        runner_lock.close()


def _recover_raced_final(
    handoff: PublishHandoff,
    fresh: SampleRenderContext,
    _fail: FailFn,
    _settle_budget: SettleFn,
    temp_dir: Path,
) -> dict[str, Any]:
    """A final dir appeared mid-render: proceed only down the verified path."""
    from services.episode_cockpit.sample_journal import (  # noqa: PLC0415 (publish-time journal at use site)
        record_sample_success_locked,
        sample_success_journaled_locked,
    )

    episode_root, identity = handoff.episode_root, handoff.identity
    try:
        existing = verify_recovery(episode_root, handoff.sample_id, identity)
        verify_sample_content(fresh.sample_ir, existing)
    except CockpitUnprocessableError as error:
        raise _fail(
            "sample-publish-conflict",
            "他の試し動画の確定と内容が食い違ったため、"
            "試し動画を確定しませんでした。",
            locked=True, conflict=True,
        ) from error
    already = sample_success_journaled_locked(episode_root, handoff.sample_id)
    if not already:
        record_sample_success_locked(
            episode_root, sample_id=handoff.sample_id, digest=handoff.digest,
            operation_id=identity.operation_id,
            sample_attempt_id=handoff.open_attempt,
            detail="recovered: success journal line was missing after publish",
        )
    settle_done = complete_missing_budget_settle_locked(
        episode_root, existing, identity)
    _settle_budget("succeeded", None)
    shutil.rmtree(temp_dir, ignore_errors=True)
    return {
        "state": "stored" if already and not settle_done else "recovering",
        "manifest": existing,
        "presentation_notes": _presentation_notes(handoff.ctx.presentation),
    }
