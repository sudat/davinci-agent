"""Sample input resolution (wave 1, P2-1 split c).

Server-side reads for one sample render: the adopted-policy context
(bundle + head + policy + full IR + presentation) resolved fresh from
the store, the server-constructed request identity (P1-5: base and
policy pins are store-derived, never client-claimed), and the next
shared-ledger budget sequence. No budget writes, no rendering here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_core import PydanticCustomError

from services.cli.review_common import mezzanine_for, store_ir
from services.compile.sample_projection import project_sample_ir, sample_total_seconds
from services.episode_cockpit.consultation_store import (
    canonical_policy_sha256,
    latest_adopted_policy,
)
from services.episode_cockpit.errors import CockpitUnprocessableError
from services.episode_cockpit.presentation_overrides import consume_presentation_intents
from services.episode_cockpit.sample_identity import SampleRequestIdentityV1
from services.foundation_io import sha256_file

if TYPE_CHECKING:
    from services.contracts.primitives import RecordFrameSpan
    from services.contracts.timeline_ir import TimelineIr0C
    from services.episode_cockpit.presentation_overrides import PresentationOverrideSet
    from services.outputs.geometry import OutputId


@dataclass(frozen=True, slots=True)
class SampleRenderContext:
    """Resolved inputs for one sample render (fresh reads, budget untouched)."""

    head_version: str
    plan_sha256: str
    policy_sha256: str | None
    full_ir_sha256: str
    sample_ir: TimelineIr0C
    total_seconds: float
    mezzanine: Path
    presentation: PresentationOverrideSet | None


type SampleRenderFn = Callable[..., None]
type SampleResolveFn = Callable[[Path, SampleRequestIdentityV1], SampleRenderContext]


def _stage_error(code: str, detail: str) -> Exception:
    from services.cli.episode_runner_rebuild import (  # noqa: PLC0415 (lazy: rebuild owns the error type)
        RebuildStageError,
    )

    return RebuildStageError(code, detail)


def default_sample_context(
    episode_root: Path, identity: SampleRequestIdentityV1
) -> SampleRenderContext:
    """Real resolver: bundle + head + policy + IR + presentation (no budget)."""
    from services.cli.episode_runner_rebuild import (  # noqa: PLC0415 (lazy: re-entry stages own these)
        BUNDLE_NAME,
        RUN_DIR_NAME,
        _bundle_or_none,
        _review_store,
        stage_plan,
    )

    bundle_file = episode_root / RUN_DIR_NAME / BUNDLE_NAME
    bundle = _bundle_or_none(bundle_file)
    if bundle is None:
        raise _stage_error(
            "sample-bundle-unreadable",
            "編集記録が読めず、試し動画を作れませんでした。",
        )
    head = stage_plan(episode_root)
    head_entry = head.index.versions.get(str(head.version))
    if (
        identity.base_version != f"v{head.version}"
        or head_entry is None
        or head_entry.plan_sha256 != identity.base_plan_sha256
    ):
        raise _stage_error(
            "sample-base-changed",
            "試し動画の依頼後に編集の版が変わりました。依頼を作り直してください。",
        )
    policy = latest_adopted_policy(episode_root)
    policy_sha = canonical_policy_sha256(policy) if policy is not None else None
    if policy_sha != identity.policy_sha256:
        raise _stage_error(
            "sample-policy-changed",
            "試し動画の依頼後に採用した方針が変わりました。依頼を作り直してください。",
        )
    plan_dir = _review_store(episode_root)[1]
    ir_path = plan_dir / f"ir-v{head.version}.json"
    try:
        ir = store_ir(ir_path)
        full_ir_sha = sha256_file(ir_path)
    except OSError as error:
        raise _stage_error(
            "sample-base-changed",
            f"編集の版が読めないため、試し動画を作れませんでした: {error}",
        ) from error
    if full_ir_sha != identity.full_ir_sha256:
        raise _stage_error(
            "sample-base-changed",
            "試し動画の依頼後に編集の版が変わりました。依頼を作り直してください。",
        )
    try:
        sample_ir = project_sample_ir(ir, list(identity.windows))
    except PydanticCustomError as error:
        raise _stage_error(f"sample-{error.type}", str(error)) from error
    try:
        presentation = consume_presentation_intents(
            episode_root,
            plan_dir,
            head_version=head.version,
            plan_version=f"v{head.version}",
        )
    except CockpitUnprocessableError as error:
        raise _stage_error(error.code, error.detail) from error
    return SampleRenderContext(
        head_version=f"v{head.version}",
        plan_sha256=head_entry.plan_sha256,
        policy_sha256=policy_sha,
        full_ir_sha256=full_ir_sha,
        sample_ir=sample_ir,
        total_seconds=sample_total_seconds(identity.windows, ir.rate),
        mezzanine=mezzanine_for(bundle_file, bundle),
        presentation=None if presentation.is_empty() else presentation,
    )


def build_sample_identity(  # noqa: PLR0913 (identity-build contract: one slot per caller field)
    episode_root: Path,
    *,
    consultation_id: str,
    judgment_id: str,
    windows: tuple[RecordFrameSpan, ...],
    operation_id: str,
    output_id: OutputId = "landscape",
) -> SampleRequestIdentityV1:
    """Server-constructed identity (P1-5): pins come from the store.

    Only consultation/judgment/windows/operation are caller-supplied;
    base version, plan hash, policy hash, and full-IR hash are read from
    the committed store — a client can never claim them.
    """
    from services.cli.episode_runner_rebuild import (  # noqa: PLC0415 (lazy: re-entry stages own these)
        BUNDLE_NAME,
        RUN_DIR_NAME,
        _bundle_or_none,
        _review_store,
        stage_plan,
    )

    bundle_file = episode_root / RUN_DIR_NAME / BUNDLE_NAME
    bundle = _bundle_or_none(bundle_file)
    if bundle is None:
        raise _stage_error(
            "sample-bundle-unreadable",
            "編集記録が読めず、試し動画を作れませんでした。",
        )
    head = stage_plan(episode_root)
    head_entry = head.index.versions.get(str(head.version))
    if head_entry is None:
        raise _stage_error(
            "sample-base-changed",
            "編集の版が読めないため、試し動画を作れませんでした。",
        )
    policy = latest_adopted_policy(episode_root)
    ir_path = _review_store(episode_root)[1] / f"ir-v{head.version}.json"
    try:
        full_ir_sha = sha256_file(ir_path)
    except OSError as error:
        raise _stage_error(
            "sample-base-changed",
            f"編集の版が読めないため、試し動画を作れませんでした: {error}",
        ) from error
    return SampleRequestIdentityV1(
        episode_id=bundle.episode_id,
        consultation_id=consultation_id,
        judgment_id=judgment_id,
        base_version=f"v{head.version}",
        base_plan_sha256=head_entry.plan_sha256,
        policy_sha256=canonical_policy_sha256(policy) if policy is not None else None,
        output_id=output_id,
        windows=windows,
        operation_id=operation_id,
        full_ir_sha256=full_ir_sha,
    )


def next_sample_sequence(episode_root: Path) -> int:
    """Next sample budget sequence past rebuild reservations AND renders."""
    from services.cli.episode_runner_rebuild import (  # noqa: PLC0415 (lazy: rebuild owns the reservation log)
        _reservation_entries,
    )
    from services.episode_cockpit.consultation_selection_budget import (  # noqa: PLC0415 (ledger read at use site)
        load_selection_budget_entries,
    )

    reserved = max((e.sequence for e in _reservation_entries(episode_root)), default=0)
    rendered = sum(
        1
        for e in load_selection_budget_entries(episode_root)
        if e.phase == "preview_reserved" and e.scope == "sample"
    )
    return reserved + rendered + 1
