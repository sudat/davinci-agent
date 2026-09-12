"""Stage-subset re-entry for applied review commands (task 9).

``--from-stage`` executes the stop-bounded lineage projection: the stages
from the given re-entry point up to ``--stop-stage`` (default ``preview``,
the ``PREVIEW_READY`` stop) run against the CURRENT committed artifacts — entering at
``selection`` re-runs the director seam under the latest adopted
consultation policy and commits the derived plan as a new review-store
version (consultation slice 2); the plan stage consumes that head version,
compile materializes its IR, and preview re-renders + re-publishes the
operator preview. Stages before the re-entry point are untouched; lineage
stages beyond ``PREVIEW_READY`` (resolve_build/qc/render) are skipped with
a log line naming the stop bound. The chain's own job store under ``run/``
is not regressed; the re-entry appends fresh per-run stage rows (uuid
idempotency keys — the task-7 mirror discipline) and, at the ``preview``
stop, publishes the SAME ``PREVIEW_READY`` job edge the chain mirror takes
(a ``PLAN_COMMITTED`` first pass lands ``PREVIEW_READY``; a job already
there or beyond it is untouched).
"""

# allow: SIZE_OK — plan-pinned single-file re-entry executor
# (stage-set projection + selection/plan/compile/preview stage fns + bundle
# hand-off + metrics writer belong to one task-9 commit scope, extended by
# the consultation slice-2 selection stage); same precedent as
# review_chat.py / review_interpreter.py. The production director/derive
# seams live in episode_runner_selection.py; the sample render flow lives
# in services/cli/sample_render.py (+ sample_resolve.py), not here.

from __future__ import annotations

import hashlib
import json as _json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final

from pydantic import Field, ValidationError

from services.cli import episode_runner_selection
from services.cli.bundle import (
    BundleDriftError,
    ReviewBundle,
    ReviewTarget,
    assemble_real_bundle,
    load_bundle,
    save_bundle,
)
from services.cli.episode_runner_state import (
    RunContext,
    block_stage,
    log_event,
    mirror_upto,
    record_stage,
)
from services.cli.episode_runner_workspace import (
    RUN_DIR_NAME,
    publish_preview,
)
from services.cli.preview_render import render_review_preview
from services.cli.project import plan_sha256
from services.cli.real_director import director_mode
from services.cli.review_common import (
    load_tools,
    mezzanine_for,
    previous_trace,
    store_ir,
    store_plan,
)
from services.contracts.primitives import StrictModel
from services.editorial.prompt import render_adopted_policy_text
from services.episode_cockpit.consultation_selection_budget import (
    DIRECTOR_WALL_ALLOWANCE_SECONDS,
    SelectionAttempt,
    attempt_for,
    has_open_director_reservation,
    ir_preview_seconds,
    reserve_director,
    reserve_preview,
    settle_director,
    settle_preview,
)
from services.episode_cockpit.consultation_store import (
    CONNECTED_POLICY_FIELDS,
    STRUCTURAL_REALIZED_CHECKS,
    AdoptedPolicyV1,
    ConsultationPolicyOutcomeV1,
    DirectorConnection,
    append_policy_outcome_once,
    canonical_policy_sha256,
    combined_wall_used_in_scope,
    ensure_preview_budget_available,
    ensure_selection_budget_available,
    full_render_authorized,
    latest_adopted_policy,
    load_budget_limits,
    now_stamp,
    policy_for_judgment,
    policy_scope_list,
    policy_summary,
    unaddressed_for_scope,
    unconfirmed_for_policy,
)
from services.episode_cockpit.errors import (
    CockpitNotFoundError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.models import RebuildRequestEntry
from services.episode_cockpit.policy_settings import (
    PolicySettingEntryV1,
    derive_policy_settings,
)
from services.episode_cockpit.presentation_overrides import (
    PresentationOverrideSet,
    consume_presentation_intents,
)
from services.episode_cockpit.review_chat import PIPELINE_STAGES
from services.foundation_io import sha256_file
from services.outputs.geometry import (
    DEFAULT_OUTPUT_ID,
    OutputId,
    review_store_relatives,
)
from services.preview.models import AppliedDecision
from services.preview.render import PREVIEW_NAME, TRACE_NAME
from services.preview.trace import rebuild_decision_id
from services.review_command.policy_commit import (
    PolicyRecoveryError,
    commit_policy,
    reuse_committed_policy,
)
from services.review_command.store import (
    HeadState,
    OperatorDecision0C,
    ReviewCommitError,
    load_head,
)

# SelectionRerunError codes that fail BEFORE any director contact (typed
# input/grant load failures) — their outcome must say not_started (渡して
# いません), never unknown.
_PRE_DIRECTOR_CONTACT_CODES = frozenset(
    {"selection-inputs-missing", "editorial-grant-invalid"}
)

if TYPE_CHECKING:
    from services.cli.episode_runner import RunnerInvocation
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIr0C
    from services.job_runner.state_store import StateStore

REENTRY_FROM_STAGES: Final = ("selection", "plan", "compile", "preview")
STOP_STAGE: Final = "preview"
CONSULTATION_STOP_STAGE: Final = "compile"
CONSULTATION_STOP_REASON: Final = "consultation-sample-pending"
BEYOND_STOP_STAGES: Final = ("resolve_build", "qc", "render")
REQUIRED_STATUS: Final = "PREVIEW_READY"
METRICS_NAME: Final = "rebuild-metrics.jsonl"
BUNDLE_NAME: Final = "review-bundle.json"
REBUILD_LOG_NAME: Final = "rebuild-requests.jsonl"


def remaining_wall_seconds_with_legacy(episode_root: Path) -> float:
    """Deadline fold = proposal wall + sample wall + legacy-history wall.

    The "full_rebuild_exempt" scope names unauthorized historical rows
    only (no live rule, no new writes): the deadline fold still reads
    them so their wall seconds stay deadline-bounded.
    """

    return load_budget_limits().wall_seconds_limit - combined_wall_used_in_scope(
        episode_root, "sample", "full_rebuild_exempt"
    )


class RebuildStageError(Exception):
    """Typed blocked-refusal carrying the operator-actionable reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


CONSULTATION_FLOW_MARKER_PREFIX: Final = "consultation-"


def is_sample_flow_run(
    applied_command: str | None, reservation_sequence: int | None
) -> bool:
    """Whether this re-entry belongs to the consultation sample flow.

    The rebuild reservation (its sequence) and the consultation marker
    distinguish sample-flow rebuilds from ordinary non-consultation
    rebuilds: a reservation sequence or a consultation-prefixed applied
    command (``consultation-{judgment}`` adoptions,
    ``consultation-flow:{command}`` NL fixes) marks the sample flow;
    plain review-command and revert markers mark ordinary rebuilds.
    """
    if reservation_sequence is not None:
        return True
    return (applied_command or "").startswith(CONSULTATION_FLOW_MARKER_PREFIX)


def refuse_unauthorized_full_preview(
    episode_root: Path,
    applied_command: str | None,
    reservation_sequence: int | None,
) -> None:
    """Refuse a sample-flow full preview without an explicit 全編へ.

    Sample-flow rebuilds never reach the full render — they terminate
    at the sample path (compile + sample surface, same 30 s
    cumulative); full renders connect only after an explicit
    full_authorized judgment. Ordinary non-consultation rebuilds pass
    through untouched.
    """
    if is_sample_flow_run(applied_command, reservation_sequence) and (
        not full_render_authorized(episode_root)
    ):
        raise RebuildStageError(
            "consultation-preview-not-authorized",
            "この相談では全編の再描画は承認されていません。"
            "試し動画で確認するか、全編へを承認してください。",
        )


def _reservation_entries(episode_root: Path) -> list[RebuildRequestEntry]:
    try:
        lines = (episode_root / REBUILD_LOG_NAME).read_bytes().splitlines()
    except OSError:
        return []
    entries: list[RebuildRequestEntry] = []
    for line in lines:
        try:
            entries.append(RebuildRequestEntry.model_validate_json(line))
        except ValidationError:
            continue
    return entries


def find_reservation(
    episode_root: Path, reservation_sequence: int
) -> RebuildRequestEntry | None:
    for entry in _reservation_entries(episode_root):
        if entry.sequence == reservation_sequence and not entry.spawned:
            return entry
    return None


def assert_reservation_fresh(  # noqa: C901 (one linear pin checklist; each check is one fail-closed refusal)
    episode_root: Path,
    reservation_sequence: int,
    *,
    job_status: str | None = None,
    deadline_monotonic: float | None = None,
    for_preview: bool = False,
) -> AdoptedPolicyV1:
    """Refuse a stale selection reservation instead of using latest policy.

    Verifies the reservation exists and is fully pinned, the pinned
    judgment still resolves to the identical policy bytes and scope, the
    judgment is still the latest adoption, the review-store head still
    sits at the reserved base version and hash, the job is still
    PREVIEW_READY and unfrozen, and the wall deadline has not passed.
    Any mismatch raises a typed refusal and commits nothing. The
    post-commit preview re-check (``for_preview``) keeps pins, policy
    identity, job, and deadline but skips the latest-adoption and base
    checks — the commit itself legitimately advanced the head.
    """

    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise RebuildStageError(
            "consultation-selection-deadline-exceeded",
            "この相談の処理時間上限に達したため、続きを確定していません。",
        )
    reservation = find_reservation(episode_root, reservation_sequence)
    if reservation is None:
        raise RebuildStageError(
            "consultation-reservation-not-found",
            "作り直しの予約記録が見つからないため、編集を始めませんでした。",
        )
    if (
        not reservation.policy_scope
        or reservation.policy_sha256 is None
        or reservation.base_plan_version is None
        or reservation.base_plan_sha256 is None
    ):
        raise RebuildStageError(
            "consultation-reservation-unpinned",
            "古い予約には対象の判断と編集版の記録が足りないため、"
            "安全に再開できません。",
        )
    if job_status == "FROZEN":
        raise RebuildStageError(
            "job-frozen",
            "この動画は確定済みのため、作り直しを行いませんでした。",
        )
    if job_status is not None and job_status not in ("PLAN_COMMITTED", REQUIRED_STATUS):
        raise RebuildStageError(
            "episode-not-preview-ready",
            f"rebuild re-entry needs {REQUIRED_STATUS}, job is at {job_status}",
        )
    if reservation.judgment_id is None:
        raise RebuildStageError(
            "consultation-reservation-unpinned",
            "古い予約には対象の判断と編集版の記録が足りないため、"
            "安全に再開できません。",
        )
    try:
        policy = policy_for_judgment(episode_root, reservation.judgment_id)
    except (CockpitNotFoundError, CockpitUnprocessableError) as error:
        raise RebuildStageError(
            "reserved-policy-changed",
            "予約した判断が変わりました。古い判断の編集は確定していません。",
        ) from error
    if (
        canonical_policy_sha256(policy) != reservation.policy_sha256
        or policy_scope_list(policy) != list(reservation.policy_scope)
    ):
        raise RebuildStageError(
            "reserved-policy-changed",
            "予約した判断が変わりました。古い判断の編集は確定していません。",
        )
    latest = latest_adopted_policy(episode_root)
    if not for_preview and (
        latest is None or latest.judgment_id != reservation.judgment_id
    ):
        raise RebuildStageError(
            "reserved-policy-changed",
            "予約した判断が変わりました。古い判断の編集は確定していません。",
        )
    if for_preview:
        return policy
    try:
        head = stage_plan(episode_root)
    except RebuildStageError as error:
        raise RebuildStageError(
            "policy-base-version-changed",
            "予約後に編集の版が変わりました。古い版を上書きしていません。",
        ) from error
    entry = head.index.versions.get(str(head.version))
    if (
        f"v{head.version}" != reservation.base_plan_version
        or entry is None
        or entry.plan_sha256 != reservation.base_plan_sha256
    ):
        raise RebuildStageError(
            "policy-base-version-changed",
            "予約後に編集の版が変わりました。古い版を上書きしていません。",
        )
    return policy


class RebuildMetricV1(StrictModel):
    """One append-only rebuild measurement line (plan §7.4).

    ``interpretation_ms`` stays null on this writer: interpretation
    happened on the apply route, which does not report timing; the single
    ``confirmations`` count is the operator's apply confirmation the
    rebuild derives from (deterministic drafts need no extra rounds).
    """

    schema_version: str = Field(default="cockpit-rebuild-metric-v1", strict=True)
    sequence: int = Field(ge=1, strict=True)
    applied_command: str = Field(min_length=1, strict=True)
    stages: tuple[str, ...]
    interpretation_ms: float | None = None
    rebuild_wall_clock_seconds: float = Field(ge=0.0, strict=True)
    unrelated_stages_skipped: tuple[str, ...]
    confirmations: int = Field(ge=0, strict=True)
    at: str = Field(min_length=1, strict=True)


@dataclass(frozen=True, slots=True)
class ReentryStages:
    executed: tuple[str, ...]
    skipped: tuple[str, ...]


@dataclass
class ReentryState:
    """Artifacts carried across the re-entry stages (head, then plan+IR)."""

    head: HeadState | None = None
    plan: EditPlan0C | None = None
    ir: TimelineIr0C | None = None
    attempt: SelectionAttempt | None = None
    presentation: PresentationOverrideSet | None = None


def reentry_stages(from_stage: str, stop_stage: str = STOP_STAGE) -> ReentryStages:
    """Stop-bounded lineage projection: [from_stage .. stop_stage] of PIPELINE_STAGES.

    The default stop is ``preview`` (every non-consultation rebuild runs to
    the re-rendered preview as before). A consultation-triggered rebuild
    passes ``stop_stage="compile"`` — the chain executes
    selection→plan→compile and TERMINATES WITHOUT stage_preview (no full
    render, no preview republish; the old preview stays and the sample
    flow is the terminal preview surface).
    """

    if from_stage not in REENTRY_FROM_STAGES:
        raise RebuildStageError(
            "from-stage-unsupported",
            f"--from-stage must be one of {REENTRY_FROM_STAGES}, got {from_stage!r}",
        )
    if stop_stage not in REENTRY_FROM_STAGES:
        raise RebuildStageError(
            "stop-stage-unsupported",
            f"--stop-stage must be one of {REENTRY_FROM_STAGES}, got {stop_stage!r}",
        )
    start = PIPELINE_STAGES.index(from_stage)
    stop = PIPELINE_STAGES.index(stop_stage)
    if stop < start:
        raise RebuildStageError(
            "stop-stage-before-from",
            f"--stop-stage {stop_stage!r} ends before --from-stage {from_stage!r}",
        )
    executed = tuple(PIPELINE_STAGES[start : stop + 1])
    return ReentryStages(
        executed=executed,
        skipped=tuple(stage for stage in PIPELINE_STAGES if stage not in executed),
    )


def stage_plan(episode_root: Path) -> HeadState:
    """Consume the LATEST committed plan version (stale-state safe)."""

    log_path, plan_dir = _review_store(episode_root)
    try:
        return load_head(log_path, plan_dir)
    except Exception as error:
        raise RebuildStageError("review-store-unreadable", str(error)) from error


def _policy_prompt_sha(policy: AdoptedPolicyV1) -> str:
    text = render_adopted_policy_text(policy_summary(policy))
    if text is None:
        raise RebuildStageError(
            "policy-prompt-unrenderable",
            "採用した方針を編集長への入力にできませんでした。",
        )
    return hashlib.sha256(text.encode()).hexdigest()


def _failed_outcome(  # noqa: PLR0913 (outcome evidence contract; kwargs are the fields)
    episode_root: Path,
    policy: AdoptedPolicyV1,
    reasons: tuple[str, ...],
    note: str,
    *,
    plan_version: str | None = None,
    reservation_sequence: int | None = None,
    run_id: str | None = None,
    commit_event_id: str | None = None,
    failure_code: str | None = None,
    director_connection: DirectorConnection | None = None,
    director_request_hash: str | None = None,
) -> None:
    append_policy_outcome_once(
        episode_root,
        ConsultationPolicyOutcomeV1(
            outcome_id=uuid.uuid4().hex[:12],
            consultation_id=policy.consultation_id,
            judgment_id=policy.judgment_id,
            proposal_id=policy.proposal_id,
            plan_version=plan_version,
            status="failed",
            reasons=reasons,
            note=note,
            created_at=now_stamp(),
            reservation_sequence=reservation_sequence,
            run_id=run_id,
            commit_event_id=commit_event_id,
            failure_code=failure_code,
            director_connection=director_connection,
            director_request_hash=director_request_hash,
            policy_prompt_sha256=_policy_prompt_sha(policy),
            connected_fields=(
                tuple(CONNECTED_POLICY_FIELDS)
                if director_connection == "confirmed"
                else ()
            ),
            realized_checks=(),
            unaddressed=unaddressed_for_scope(policy),
            unconfirmed=unconfirmed_for_policy(policy),
        ),
    )


def _refusal_policy(
    episode_root: Path,
    reservation_sequence: int,
    fallback: AdoptedPolicyV1 | None,
) -> AdoptedPolicyV1 | None:
    """Best-effort outcome context for a freshness refusal (never raises)."""

    reservation = find_reservation(episode_root, reservation_sequence)
    if reservation is not None and reservation.judgment_id is not None:
        try:
            return policy_for_judgment(episode_root, reservation.judgment_id)
        except (CockpitNotFoundError, CockpitUnprocessableError):
            pass
    return latest_adopted_policy(episode_root) or fallback


def _refusal_with_outcome(  # noqa: PLR0913 (refusal evidence contract; kwargs are the fields)
    episode_root: Path,
    policy: AdoptedPolicyV1,
    reservation_sequence: int | None,
    run_id: str | None,
    error: RebuildStageError,
    *,
    director_connection: DirectorConnection,
    director_request_hash: str | None = None,
) -> RebuildStageError:
    """Record one failed outcome for a freshness refusal, then re-raise it."""

    _failed_outcome(
        episode_root, policy, (f"{error.code}: {error.detail}",), error.detail,
        reservation_sequence=reservation_sequence,
        run_id=run_id,
        failure_code=error.code,
        director_connection=director_connection,
        director_request_hash=director_request_hash,
    )
    return RebuildStageError(error.code, error.detail)


def _connected_outcome(  # noqa: PLR0913 (outcome evidence contract; kwargs are the fields)
    episode_root: Path,
    policy: AdoptedPolicyV1,
    *,
    plan_version: str,
    reservation_sequence: int | None,
    run_id: str | None,
    commit_event_id: str | None,
    director_request_hash: str | None,
    reused: bool = False,
    applied_settings: tuple[PolicySettingEntryV1, ...] = (),
    settings_unaddressed: tuple[str, ...] = (),
    superseded_by_head: bool | None = None,
) -> None:
    append_policy_outcome_once(
        episode_root,
        ConsultationPolicyOutcomeV1(
            outcome_id=uuid.uuid4().hex[:12],
            consultation_id=policy.consultation_id,
            judgment_id=policy.judgment_id,
            proposal_id=policy.proposal_id,
            plan_version=plan_version,
            status="connected",
            reasons=(
                ("reused sealed policy commit without re-running the director",)
                if reused
                else (f"adopted {policy.decision} policy re-run by the director",)
            ),
            note=(
                "確定済みの版を再利用しました。反映の検証は構造検証のみ。"
                if reused
                else "採用した方針を編集長への入力に接続しました。"
                "反映の検証は構造検証のみ "
                "(プランナー充足性・生成・コンパイル・版再読込)。"
            ),
            created_at=now_stamp(),
            reservation_sequence=reservation_sequence,
            run_id=run_id,
            commit_event_id=commit_event_id,
            failure_code=None,
            director_connection="confirmed",
            director_request_hash=director_request_hash,
            policy_prompt_sha256=_policy_prompt_sha(policy),
            connected_fields=tuple(CONNECTED_POLICY_FIELDS),
            realized_checks=tuple(STRUCTURAL_REALIZED_CHECKS),
            unaddressed=tuple(unaddressed_for_scope(policy)) + tuple(settings_unaddressed),
            unconfirmed=unconfirmed_for_policy(policy),
            applied_settings=tuple(applied_settings),
            superseded_by_head=superseded_by_head,
        ),
    )


def _director_interpretable(env: dict[str, str], runtime_path: Path | None) -> bool:
    """Whether the resolved director path can interpret an adopted policy.

    The deterministic-baseline director cannot (honest typed refusal
    upstream); both live paths (metered openai-api, flat-rate codex-exec)
    can. The resolved editorial runtime decides — never the key alone.
    """

    return director_mode(env, runtime_path) != "deterministic-baseline"


def stage_selection(  # noqa: PLR0913, C901, PLR0912, PLR0915 (selection stage: pin/budget/director/derive/commit checkpoints in one stage fn)
    episode_root: Path,
    log: BinaryIO,
    *,
    policy: AdoptedPolicyV1 | None = None,
    reservation_sequence: int | None = None,
    run_id: str | None = None,
    job_status: str | None = None,
    deadline_monotonic: float | None = None,
    runtime_path: Path | None = None,
) -> str:
    """Re-run the director under the adopted policy; commit a new version.

    The §9.1:406 loop: load the review-store head, re-run the initial
    chain's director seam with the adopted policy as constraint input,
    derive + validate the plan through the chain's own pure functions, and
    commit it as a NEW review-store version via the review-command commit
    path. ANY failure appends a failed outcome (never a silent drop),
    marks the stage failed via the existing block path, and commits no
    version — the consultation UI then shows the failure (相談へ戻る).
    With a reservation, the same freshness check runs before the director
    call and again right before the commit — never the latest policy.
    """

    if reservation_sequence is not None:
        if job_status == "FROZEN":
            raise RebuildStageError(
                "job-frozen",
                "この動画は確定済みのため、作り直しを行いませんでした。",
            )
        log_path, plan_dir = _review_store(episode_root)
        pending = find_reservation(episode_root, reservation_sequence)
        if pending is not None and pending.judgment_id is not None:
            reused = reuse_committed_policy(
                log_path, plan_dir, pending.judgment_id
            )
            if reused is not None:
                try:
                    reused_policy = policy_for_judgment(
                        episode_root, pending.judgment_id
                    )
                except (CockpitNotFoundError, CockpitUnprocessableError):
                    reused_policy = None
                if reused_policy is not None:
                    _connected_outcome(
                        episode_root, reused_policy,
                        plan_version=f"v{reused.version}",
                        reservation_sequence=reservation_sequence,
                        run_id=run_id,
                        commit_event_id=reused.event_id,
                        director_request_hash=None,
                        reused=True,
                        superseded_by_head=reused.superseded_by_head,
                    )
                    log_event(
                        log, "policy_selection_committed",
                        policy=pending.judgment_id,
                        plan_version=f"v{reused.version}",
                        reservation_sequence=reservation_sequence,
                        run_id=run_id,
                        idempotent=True,
                    )
                    return plan_sha256(load_head(log_path, plan_dir).plan)
        try:
            policy = assert_reservation_fresh(
                episode_root,
                reservation_sequence,
                job_status=job_status,
                deadline_monotonic=deadline_monotonic,
            )
        except RebuildStageError as error:
            outcome_policy = _refusal_policy(
                episode_root, reservation_sequence, policy
            )
            if outcome_policy is None:
                raise
            raise _refusal_with_outcome(
                episode_root, outcome_policy, reservation_sequence, run_id,
                error, director_connection="not_started",
            ) from error
        attempt: SelectionAttempt | None = attempt_for(
            reservation_sequence, policy.consultation_id, policy.judgment_id
        )
    else:
        attempt = None
    if policy is None:
        policy = latest_adopted_policy(episode_root)
        if policy is None:
            raise RebuildStageError(
                "no-adopted-policy",
                "selection re-entry needs an adopted consultation policy; "
                "none is on the table",
            )
    request_hash: str | None = None
    if attempt is not None and has_open_director_reservation(
        episode_root, attempt.attempt_id
    ):
        reason = "consultation-selection-attempt-uncertain"
        _failed_outcome(
            episode_root, policy, (reason,),
            "前回のAI処理が完了したか確認できないため、"
            "重複利用を避けて再実行を止めました。",
            reservation_sequence=reservation_sequence,
            run_id=run_id,
            failure_code=reason,
            director_connection="unknown",
        )
        raise RebuildStageError(
            reason,
            "前回のAI処理が完了したか確認できないため、"
            "重複利用を避けて再実行を止めました。",
        )
    stage_plan(episode_root)
    if not _director_interpretable(dict(os.environ), runtime_path):
        reason = "編集長が決定論化モードのため方針を解釈できませんでした"
        _failed_outcome(
            episode_root, policy, (reason,),
            "方針の解釈も検証も行っていません。公開モデル runtime で再実行してください。",
            reservation_sequence=reservation_sequence,
            run_id=run_id,
            failure_code="policy-not-interpretable",
            director_connection="not_started",
        )
        raise RebuildStageError("policy-not-interpretable", reason)
    wall_allowance = 0.0
    if attempt is not None:
        try:
            remaining = ensure_selection_budget_available(
                episode_root, load_budget_limits()
            )
        except (CockpitNotFoundError, CockpitUnprocessableError) as error:
            code = (
                error.code
                if isinstance(error, CockpitUnprocessableError)
                else "consultation-selection-budget-exhausted"
            )
            _failed_outcome(
                episode_root, policy, (f"{code}: {error}",), str(error),
                reservation_sequence=reservation_sequence,
                run_id=run_id,
                failure_code=code,
                director_connection="not_started",
            )
            raise RebuildStageError(code, str(error)) from error
        wall_allowance = min(DIRECTOR_WALL_ALLOWANCE_SECONDS, remaining)
        reserve_director(episode_root, attempt, wall_allowance)
    director_started = time.monotonic()
    try:
        rerun = episode_runner_selection.rerun_director_with_policy(
            episode_root, policy, dict(os.environ), runtime_path
        )
    except episode_runner_selection.SelectionRerunError as error:
        if attempt is not None:
            settle_director(
                episode_root, attempt,
                wall_elapsed=max(time.monotonic() - director_started, 0.001),
                result="failed", failure_code=error.code,
            )
        _failed_outcome(
            episode_root, policy, (f"{error.code}: {error.detail}",),
            "監督の再実行に失敗したため、方針の検証を行っていません。",
            reservation_sequence=reservation_sequence,
            run_id=run_id,
            failure_code=error.code,
            director_connection=(
                "not_started"
                if error.code in _PRE_DIRECTOR_CONTACT_CODES
                else "confirmed"
                if error.code == "director_refused"
                else "unknown"
            ),
        )
        raise RebuildStageError(error.code, error.detail) from error
    request_hash = rerun.outcome.request_hash
    if attempt is not None:
        settle_director(
            episode_root, attempt,
            wall_elapsed=max(time.monotonic() - director_started, 0.001),
            result="succeeded",
        )
    try:
        new_plan = episode_runner_selection.derive_policy_plan(episode_root, rerun, policy)
    except episode_runner_selection.PolicyDerivationError as error:
        _failed_outcome(
            episode_root, policy, (f"{error.code}: {error.detail}",),
            "計画の導出・検証に失敗したため、版を確定していません。",
            reservation_sequence=reservation_sequence,
            run_id=run_id,
            failure_code=error.code,
            director_connection="confirmed",
            director_request_hash=request_hash,
        )
        raise RebuildStageError(error.code, error.detail) from error
    log_path, plan_dir = _review_store(episode_root)
    if reservation_sequence is not None:
        try:
            policy = assert_reservation_fresh(
                episode_root,
                reservation_sequence,
                job_status=job_status,
                deadline_monotonic=deadline_monotonic,
            )
        except RebuildStageError as error:
            raise _refusal_with_outcome(
                episode_root, policy, reservation_sequence, run_id, error,
                director_connection="confirmed",
                director_request_hash=request_hash,
            ) from error
    decision = OperatorDecision0C(
        decision_id=f"dec-policy-{policy.judgment_id}",
        actor_intent="operator",
        note=f"adopted consultation policy {policy.judgment_id} ({policy.decision})",
    )
    reservation = (
        find_reservation(episode_root, reservation_sequence)
        if reservation_sequence is not None
        else None
    )
    try:
        outcome = commit_policy(
            new_plan, decision, log_path, plan_dir,
            judgment_id=policy.judgment_id,
            proposal_id=policy.proposal_id,
            policy_decision=policy.decision,
            expected_base_version=(
                reservation.base_plan_version if reservation is not None else None
            ),
            expected_base_plan_sha256=(
                reservation.base_plan_sha256 if reservation is not None else None
            ),
        )
        verified = load_head(log_path, plan_dir)
    except PolicyRecoveryError as error:
        if error.recorded_version is not None:
            _failed_outcome(
                episode_root, policy,
                (f"{error.code}: {error}",),
                f"編集の記録は v{error.recorded_version} まで残っていますが、"
                "版ファイルの回復を確認できません。自動で「反映されていない」とは判定しません。",
                plan_version=f"v{error.recorded_version}",
                reservation_sequence=reservation_sequence,
                run_id=run_id,
                failure_code=error.code,
                director_connection="confirmed",
                director_request_hash=request_hash,
            )
        else:
            _failed_outcome(
                episode_root, policy, (f"{error.code}: {error}",),
                "版の確定前に失敗しました。方針を反映した版はありません。",
                reservation_sequence=reservation_sequence,
                run_id=run_id,
                failure_code=error.code,
                director_connection="confirmed",
                director_request_hash=request_hash,
            )
        raise RebuildStageError(error.code, str(error)) from error
    except Exception as error:
        code = error.code if isinstance(error, ReviewCommitError) else "policy-commit-failed"
        _failed_outcome(
            episode_root, policy, (f"{code}: {error}",),
            "版の確定に失敗したため、方針は反映されていません。",
            reservation_sequence=reservation_sequence,
            run_id=run_id,
            failure_code=code,
            director_connection="confirmed",
            director_request_hash=request_hash,
        )
        raise RebuildStageError(code, str(error)) from error
    settings = derive_policy_settings(policy)
    _connected_outcome(
        episode_root, policy,
        plan_version=f"v{outcome.version}",
        reservation_sequence=reservation_sequence,
        run_id=run_id,
        commit_event_id=outcome.event_id,
        director_request_hash=request_hash,
        applied_settings=settings.entries,
        settings_unaddressed=settings.unaddressed,
        superseded_by_head=outcome.superseded_by_head,
    )
    log_event(
        log, "policy_selection_committed", policy=policy.judgment_id,
        plan_version=f"v{outcome.version}",
        reservation_sequence=reservation_sequence,
        run_id=run_id,
    )
    return plan_sha256(verified.plan)


def stage_compile(episode_root: Path, head: HeadState) -> tuple[EditPlan0C, TimelineIr0C]:
    """Materialize the head version's plan + IR (hash-verified inside load_head)."""

    _, plan_dir = _review_store(episode_root)
    try:
        return (
            store_plan(plan_dir / f"plan-v{head.version}.json"),
            store_ir(plan_dir / f"ir-v{head.version}.json"),
        )
    except Exception as error:
        raise RebuildStageError("ir-unreadable", str(error)) from error


def _load_or_bootstrap_bundle(bundle_file: Path, log: BinaryIO) -> ReviewBundle | None:
    """Load the review bundle; bootstrap it when a render skipped its write.

    ``None`` means the bundle genuinely does not exist (the PLAN_COMMITTED
    first-render state) — no preview on disk for the bootstrap to pin.
    """

    try:
        return load_bundle(bundle_file)
    except BundleDriftError as error:
        if error.code != "bundle_unreadable":
            raise
        bundle = _bootstrap_bundle(bundle_file)
        if bundle is not None:
            log_event(log, "review_bundle_bootstrapped", bundle=str(bundle_file))
        return None


def _render_inputs_and_decision(
    episode_root: Path,
    bundle_file: Path,
    bundle: ReviewBundle | None,
    head: HeadState,
    run_id: str,
) -> tuple[Path, AppliedDecision | None]:
    """(mezzanine, decision) for one stage_preview render.

    A PLAN_COMMITTED first pass never built the bundle (a PREVIEW_READY
    artifact: its target pins rendered preview hashes), so the 全編へ
    continuation's first render resolves its mezzanine from the pre-render
    set and carries NO decision — there is no prior preview to diff
    against.
    """

    if bundle is None:
        from services.cli.sample_resolve import (  # noqa: PLC0415 (shared pre-render resolver)
            pre_render_episode_inputs,
        )

        resolved = pre_render_episode_inputs(episode_root, bundle_file, None)
        if resolved is None:
            raise RebuildStageError(
                "preview-failed",
                "編集の元素材が読めず、全編の描画を始められませんでした。",
            )
        return resolved[1], None
    head_entry = head.index.versions.get(str(head.version))
    decision = AppliedDecision(
        # r9d fix: run-id suffix — intent-only applies never bump the head version.
        decision_id=rebuild_decision_id(bundle.episode_id, head.version, run_id),
        case_id=bundle.episode_id,
        classification="clear",
        plan_version_after=f"v{head.version}",
        plan_sha256=head_entry.plan_sha256 if head_entry is not None else None,
        previous_trace=previous_trace(bundle_file, bundle),
    )
    return mezzanine_for(bundle_file, bundle), decision


def stage_preview(  # noqa: PLR0913 (preview stage: budget-gate classification + render + settle in one stage fn, like stage_selection)
    episode_root: Path,
    head: HeadState,
    plan: EditPlan0C,
    ir: TimelineIr0C,
    log: BinaryIO,
    *,
    run_id: str,
    selection_attempt: SelectionAttempt | None = None,
    preview_timeout_seconds: float | None = None,
    presentation: PresentationOverrideSet | None = None,
) -> str:
    """Re-render the review-plane preview from the new version + republish.

    A selection attempt reserves its exact sample seconds before the
    renderer starts and settles them afterwards: once rendering starts
    the reserved seconds are charged even on failure (with the measured
    wall time), so the 30 s allowance can never be silently exceeded.
    A render whose sample seconds exceed the remaining allowance is
    REFUSED here with the typed consultation-preview-budget-exhausted
    error — the pre-authorization loop renders the ≤30 s sample path
    instead, and full renders start only after the operator's explicit
    全編へ judgment under the separate full_episode ledger. No new
    "full_rebuild_exempt" rows are ever written (unauthorized historical
    rows stay readable for the wall fold only).
    """

    attempt = selection_attempt
    preview_seconds = 0.0
    preview_started = 0.0
    if attempt is not None:
        preview_seconds = ir_preview_seconds(ir)
        try:
            ensure_preview_budget_available(
                episode_root, load_budget_limits(), preview_seconds
            )
        except CockpitUnprocessableError as error:
            raise RebuildStageError(error.code, str(error)) from error
        except CockpitNotFoundError as error:
            raise RebuildStageError(
                "consultation-preview-budget-exhausted", str(error)
            ) from error
        reserve_preview(episode_root, attempt, preview_seconds)
        preview_started = time.monotonic()
    run_dir = episode_root / RUN_DIR_NAME
    bundle_file = run_dir / BUNDLE_NAME
    try:
        bundle = _load_or_bootstrap_bundle(bundle_file, log)
        first_render = bundle is None
        preview_dir = run_dir / f"preview-v{head.version}"
        mezzanine, decision = _render_inputs_and_decision(
            episode_root, bundle_file, bundle, head, run_id
        )
        render_review_preview(
            plan, ir, mezzanine, preview_dir,
            tools=load_tools(), decision=decision,
            timeout_seconds=preview_timeout_seconds,
            presentation=presentation.to_render_settings() if presentation is not None else None,
            presentation_trace=(
                presentation.to_trace_presentation() if presentation is not None else None
            ),
        )
    except Exception as error:
        if attempt is not None:
            settle_preview(
                episode_root, attempt,
                preview_seconds=preview_seconds,
                wall_elapsed=max(time.monotonic() - preview_started, 0.001),
                result="failed",
                failure_code=(
                    error.code if isinstance(error, RebuildStageError) else "preview-failed"
                ),
            )
        raise RebuildStageError("preview-failed", str(error)) from error
    if attempt is not None:
        settle_preview(
            episode_root, attempt,
            preview_seconds=preview_seconds,
            wall_elapsed=max(time.monotonic() - preview_started, 0.001),
            result="succeeded",
        )
    preview_sha = sha256_file(run_dir / f"preview-v{head.version}" / PREVIEW_NAME)
    if first_render:
        bootstrapped = _bootstrap_bundle(bundle_file, version=head.version)
        if bootstrapped is None:
            raise RebuildStageError(
                "preview-failed",
                "全編の描画後に編集記録を書けませんでした。",
            )
        _update_bundle(bundle_file, bootstrapped, head, preview_sha)
    else:
        _update_bundle(bundle_file, bundle, head, preview_sha)
    publish_preview(episode_root, log, source_dir=f"preview-v{head.version}", run_id=run_id)
    return preview_sha


def _consume_presentation(
    episode_root: Path, carried: ReentryState, log: BinaryIO, run_id: str
) -> None:
    """Derive + manifest-record the journal's presentation intents (r9e gap).

    Runs wherever compile re-materializes plan+IR: the override set is
    stashed on the carried state for the preview stage, and the honest
    unimplemented notes ride the runner log as typed lines.
    """

    if carried.head is None or carried.presentation is not None:
        return
    _, plan_dir = _review_store(episode_root)
    override_set = consume_presentation_intents(
        episode_root,
        plan_dir,
        head_version=carried.head.version,
        plan_version=f"v{carried.head.version}",
    )
    carried.presentation = override_set
    if override_set.is_empty():
        return
    log_event(
        log, "presentation_overrides", run_id=run_id,
        applied_commands=list(override_set.applied_command_ids()),
        overrides=[
            {
                "command_id": entry.command_id,
                "command_kind": entry.command_kind,
                "setting": entry.setting,
                "value": entry.value,
            }
            for entry in override_set.overrides
        ],
        unimplemented=[
            {"command_id": note.command_id, "command_kind": note.command_kind,
             "code": note.code, "detail": note.detail}
            for note in override_set.notes
        ],
    )


def _review_store(
    episode_root: Path, output_id: OutputId = DEFAULT_OUTPUT_ID
) -> tuple[Path, Path]:
    log_rel, store_rel = review_store_relatives(output_id)
    return (
        episode_root.joinpath(*log_rel),
        episode_root.joinpath(*store_rel),
    )


def _update_bundle(
    bundle_file: Path, bundle: ReviewBundle, head: HeadState, preview_sha: str
) -> None:
    """Point the review bundle at the rebuilt head (store paths → cockpit layout)."""

    log_path, plan_dir = _review_store(bundle_file.parent.parent)
    preview_dir = f"preview-v{head.version}"
    target = ReviewTarget(
        plan_version=f"v{head.version}",
        plan_sha256=sha256_file(plan_dir / f"plan-v{head.version}.json"),
        ir_sha256=sha256_file(plan_dir / f"ir-v{head.version}.json"),
        preview_dir=preview_dir,
        preview_sha256=preview_sha,
        trace_sha256=sha256_file(bundle_file.parent / preview_dir / TRACE_NAME),
    )
    applied = tuple(
        dict.fromkeys(
            (
                *bundle.applied_event_ids,
                *(event.event_id for event in head.events if event.kind == "decision_applied"),
            )
        )
    )
    save_bundle(
        bundle.model_copy(
            update={
                # relpath (not Path.relative_to): the cockpit review store
                # is a SIBLING of run/, so the bundle must carry resolvable
                # ../review/... paths — relative_to cannot emit "..", which
                # crashed the real rebuild preview stage (T10 live catch).
                "store_dir": os.path.relpath(plan_dir, bundle_file.parent),
                "events_log": os.path.relpath(log_path, bundle_file.parent),
                "current": target,
                "applied_event_ids": applied,
            }
        ),
        bundle_file,
    )


def _bootstrap_bundle(bundle_file: Path, version: int = 1) -> ReviewBundle | None:  # noqa: C901, PLR0912
    """Bootstrap run/review-bundle.json when a manual render skipped its write.

    Mirrors the review-store bootstrap's resilience class: render_preview_tail's
    save_bundle was bypassed, so the rebuild re-entry reconstructs the bundle
    with identical assemble_real_bundle semantics from the on-disk artifacts.
    ``version`` selects which preview-vN/plan-vN/ir-vN triple the target pins
    (the first-render path bootstraps at the head version, not always v1).
    """

    run_dir = bundle_file.parent
    episode_dir = run_dir.parent
    mezzanine = run_dir / "media" / "edit-source.mov"
    preview_dir = run_dir / f"preview-v{version}"
    preview = preview_dir / PREVIEW_NAME
    trace = preview_dir / TRACE_NAME
    episode_manifest = run_dir / "episode.json"
    resolved_policy = run_dir / "resolved-policy.json"
    source_manifest = run_dir / "source-manifest.json"
    orchestration = run_dir / "episode" / "analyze-state" / "orchestration-state.json"
    cockpit_plan = episode_dir / "review" / "store" / f"plan-v{version}.json"
    cockpit_ir = episode_dir / "review" / "store" / f"ir-v{version}.json"
    for required in (
        mezzanine,
        preview,
        trace,
        episode_manifest,
        source_manifest,
        cockpit_plan,
        cockpit_ir,
    ):
        if not required.is_file():
            return None
    try:
        episode_payload = _json.loads(episode_manifest.read_bytes())
        episode_id: str = episode_payload["episode_id"]
        source_payload = _json.loads(source_manifest.read_bytes())
        eligibility_status: str = source_payload.get("eligibility", {}).get(
            "verdict", "supported"
        )
        if eligibility_status not in ("supported", "assisted", "unsupported"):
            eligibility_status = "supported"
    except (OSError, ValueError, KeyError):
        return None
    edit_source_world_sha256: str | None = None
    try:
        if orchestration.is_file():
            orch = _json.loads(orchestration.read_bytes())
            for binding in orch.get("bindings", {}).values():
                candidate = binding.get("edit_source_sha256")
                if isinstance(candidate, str) and len(candidate) == 64:  # noqa: PLR2004
                    edit_source_world_sha256 = candidate
                    break
    except (OSError, ValueError):
        pass
    if edit_source_world_sha256 is None:
        return None
    policy_sha: str | None = None
    for candidate_path in (resolved_policy, Path("config/gates/phase-0c-v1.json")):
        if candidate_path.is_file():
            try:
                policy_sha = sha256_file(candidate_path)
                break
            except OSError:
                continue
    if policy_sha is None:
        return None
    try:
        target = ReviewTarget(
            plan_version=f"v{version}",
            plan_sha256=sha256_file(cockpit_plan),
            ir_sha256=sha256_file(cockpit_ir),
            preview_dir=f"preview-v{version}",
            preview_sha256=sha256_file(preview),
            trace_sha256=sha256_file(trace),
        )
        bundle = assemble_real_bundle(
            episode_id=episode_id,
            eligibility_status=eligibility_status,  # type: ignore[arg-type]
            mezzanine_sha256=sha256_file(mezzanine),
            edit_source_world_sha256=edit_source_world_sha256,
            episode_manifest_sha256=sha256_file(episode_manifest),
            policy_sha256=policy_sha,
            target=target,
        )
    except OSError:
        return None
    save_bundle(bundle, bundle_file)
    return bundle


def _execute(  # noqa: PLR0913 (re-entry wiring: store/ctx/root/stages + reservation binding)
    store: StateStore,
    ctx: RunContext,
    episode_root: Path,
    stages: ReentryStages,
    *,
    reservation_sequence: int | None = None,
    applied_command: str | None = None,
    job_status: str | None = None,
    defer_terminal_success: bool = False,
    editorial_runtime: Path | None = None,
) -> tuple[str, str] | None:
    """Run the re-entry stages; returns the deferred terminal (stage, adopted).

    When ``defer_terminal_success`` is set, the FINAL executed stage's work
    still runs (render + publish included) but its ``succeeded`` row + log
    line are NOT recorded here — the (stage, adopted) pair is returned so
    the caller can record it only AFTER durably writing the
    rebuild-metrics record (metrics-before-completion: the observable
    完了 state must never precede its recorded evidence). Failures behave
    as before (the frontier is blocked, the exception propagates, nothing
    is deferred). Without deferral (or with no executed stages) returns
    None.
    """
    carried = ReentryState()
    last_index = len(stages.executed) - 1
    for index, stage in enumerate(stages.executed):
        record_stage(store, ctx, stage, "running")
        log_event(ctx.log, "rebuild_stage", run_id=ctx.run_id, stage=stage, status="running")
        try:
            adopted = _run_stage(
                stage,
                episode_root,
                carried,
                ctx.log,
                run_id=ctx.run_id,
                reservation_sequence=reservation_sequence,
                applied_command=applied_command,
                job_status=job_status,
                editorial_runtime=editorial_runtime,
            )
        except RebuildStageError as error:
            block_stage(store, ctx, stage, error.code)
            log_event(
                ctx.log, "rebuild_stage", run_id=ctx.run_id, stage=stage,
                status="failed_blocked", code=error.code,
            )
            raise
        if defer_terminal_success and index == last_index:
            return stage, adopted
        record_stage(store, ctx, stage, "succeeded", adopted=adopted)
        log_event(ctx.log, "rebuild_stage", run_id=ctx.run_id, stage=stage, status="succeeded")
    return None


def _run_stage(  # noqa: PLR0913, C901, PLR0912 (stage dispatch: stage/root/state/log + run/reservation/preview binding)
    stage: str,
    episode_root: Path,
    carried: ReentryState,
    log: BinaryIO,
    *,
    run_id: str,
    reservation_sequence: int | None = None,
    applied_command: str | None = None,
    job_status: str | None = None,
    editorial_runtime: Path | None = None,
) -> str:
    """One re-entry stage against the carried state; returns its adopted hash.

    Entering at compile/preview hydrates the missing head/IR itself (the
    review store is an idempotent read), so every legal ``--from-stage``
    value is self-sufficient without re-running earlier stages. A
    selection     re-entry with a reservation executes the pinned judgment
    (never the latest policy); without one it keeps the legacy
    latest-policy behavior.
    """

    if reservation_sequence is not None and stage != "selection":
        # W4: the remaining wall budget bounds EVERY re-entry path, not
        # just the director call and the preview render — a reserved run
        # whose allowance is already spent stops before plan/compile work.
        # The fold also reads unauthorized historical rows, so their wall
        # seconds stay deadline-bounded.
        remaining = remaining_wall_seconds_with_legacy(episode_root)
        if remaining <= 0:
            raise RebuildStageError(
                "consultation-selection-deadline-exceeded",
                "この相談の処理時間上限に達したため、続きを確定していません。",
            )
    if stage == "selection":
        if reservation_sequence is not None:
            remaining = remaining_wall_seconds_with_legacy(episode_root)
            deadline = (
                time.monotonic() + remaining if remaining > 0 else None
            )
            reservation = find_reservation(episode_root, reservation_sequence)
            if reservation is not None and reservation.judgment_id is not None:
                try:
                    pinned = policy_for_judgment(
                        episode_root, reservation.judgment_id
                    )
                except (CockpitNotFoundError, CockpitUnprocessableError):
                    pinned = None
                if pinned is not None:
                    carried.attempt = attempt_for(
                        reservation_sequence,
                        pinned.consultation_id,
                        pinned.judgment_id,
                    )
            adopted = stage_selection(
                episode_root,
                log,
                policy=None,
                reservation_sequence=reservation_sequence,
                run_id=run_id,
                job_status=job_status,
                deadline_monotonic=deadline,
                runtime_path=editorial_runtime,
            )
        else:
            policy = latest_adopted_policy(episode_root)
            if policy is None:
                raise RebuildStageError(
                    "no-adopted-policy",
                    "selection re-entry needs an adopted consultation policy; "
                    "none is on the table",
                )
            adopted = stage_selection(
                episode_root, log, policy=policy, runtime_path=editorial_runtime
            )
        carried.head = stage_plan(episode_root)
        carried.plan = None
        carried.ir = None
        return adopted
    if stage == "plan":
        carried.head = stage_plan(episode_root)
        return plan_sha256(carried.head.plan)
    if carried.head is None:
        carried.head = stage_plan(episode_root)
    if stage == "compile":
        carried.plan, carried.ir = stage_compile(episode_root, carried.head)
        _consume_presentation(episode_root, carried, log, run_id)
        return carried.head.index.versions[str(carried.head.version)].ir_sha256
    refuse_unauthorized_full_preview(
        episode_root, applied_command, reservation_sequence
    )
    if carried.plan is None or carried.ir is None:
        carried.plan, carried.ir = stage_compile(episode_root, carried.head)
        _consume_presentation(episode_root, carried, log, run_id)
    preview_timeout: float | None = None
    if carried.attempt is not None and reservation_sequence is not None:
        assert_reservation_fresh(
            episode_root, reservation_sequence, job_status=job_status,
            for_preview=True,
        )
        remaining = remaining_wall_seconds_with_legacy(episode_root)
        if remaining <= 0:
            raise RebuildStageError(
                "consultation-selection-deadline-exceeded",
                "この相談の処理時間上限に達したため、続きを確定していません。",
            )
        preview_timeout = remaining
    return stage_preview(
        episode_root, carried.head, carried.plan, carried.ir, log, run_id=run_id,
        selection_attempt=carried.attempt, preview_timeout_seconds=preview_timeout,
        presentation=carried.presentation,
    )


def run_reentry(store: StateStore, ctx: RunContext, call: RunnerInvocation, log: BinaryIO) -> int:
    """Execute one validated --from-stage re-entry; returns the exit code.

    Metrics-before-completion: the rebuild-metrics record is durably
    appended BEFORE the terminal preview ``succeeded`` row (the state
    transition that surfaces     the observable 再build完了) is recorded, so
    no poll can ever observe 完了 without its recorded evidence.
    ``wall_seconds`` covers the stage execution itself — measured up to
    the metrics write, excluding only the terminal bookkeeping row/log
    writes that follow it (the metrics write itself is likewise excluded,
    since writing it first is the whole point). A full-stop run then
    publishes the ``PREVIEW_READY`` job edge through the same mirror the
    chain path uses (a ``PLAN_COMMITTED`` first pass lands
    ``PREVIEW_READY``).

    ``call.stop_stage`` truncates the chain: a consultation-triggered
    rebuild stops at ``compile`` — the candidate plan is committed to the
    review store, no preview is re-rendered or republished, and the job
    keeps its pre-rebuild ``PREVIEW_READY`` state. The ``rebuild_finished``
    line then carries ``stopped_at`` + ``reason`` instead of claiming a
    preview run.
    """

    from services.cli.episode_runner import EXIT_SUCCESS  # noqa: PLC0415 (avoids import cycle)

    stop_stage = call.stop_stage or STOP_STAGE
    stages = reentry_stages(
        call.from_stage if call.from_stage is not None else "", stop_stage
    )
    for stage in BEYOND_STOP_STAGES:
        log_event(
            log, "rebuild_stage_skipped", run_id=ctx.run_id, stage=stage,
            reason=f"beyond {call.stop} stop",
        )
    if stop_stage != STOP_STAGE:
        for skipped in REENTRY_FROM_STAGES[REENTRY_FROM_STAGES.index(stop_stage) + 1:]:
            log_event(
                log, "rebuild_stage_skipped", run_id=ctx.run_id, stage=skipped,
                reason=f"consultation stop at {stop_stage} ({CONSULTATION_STOP_REASON})",
            )
    snapshot = store.get_job_snapshot(ctx.job_id)
    if snapshot.job.status == "FROZEN":
        raise RebuildStageError(
            "job-frozen",
            "この動画は確定済みのため、作り直しを行いませんでした。",
        )
    if snapshot.job.status not in ("PLAN_COMMITTED", REQUIRED_STATUS):
        raise RebuildStageError(
            "episode-not-preview-ready",
            f"rebuild re-entry needs {REQUIRED_STATUS}, job is at {snapshot.job.status}",
        )
    started = time.monotonic()
    deferred = _execute(
        store,
        ctx,
        call.episode_root,
        stages,
        reservation_sequence=call.reservation_sequence,
        applied_command=call.applied_command,
        job_status=snapshot.job.status,
        defer_terminal_success=True,
        editorial_runtime=call.editorial_runtime,
    )
    wall = time.monotonic() - started
    if call.applied_command is not None:
        _append_metric(call.episode_root, call.applied_command, stages, wall)
    if deferred is not None:
        terminal_stage, terminal_adopted = deferred
        record_stage(store, ctx, terminal_stage, "succeeded", adopted=terminal_adopted)
        log_event(
            log, "rebuild_stage", run_id=ctx.run_id, stage=terminal_stage,
            status="succeeded",
        )
        if stop_stage == STOP_STAGE:
            # The chain path publishes the PREVIEW_READY job edge through
            # the live mirror (mirror_upto); a re-entry owns no watcher, so
            # it takes the SAME edge itself — the 全編へ continuation from a
            # PLAN_COMMITTED first pass must land the job at PREVIEW_READY
            # (a no-op when the job is already there or beyond it).
            mirror_upto(store, ctx, REQUIRED_STATUS, terminal_adopted)
    if stop_stage != STOP_STAGE:
        log_event(
            log, "rebuild_finished", run_id=ctx.run_id,
            stages=list(stages.executed), wall_seconds=round(wall, 3),
            stopped_at=stop_stage, reason=CONSULTATION_STOP_REASON,
        )
    else:
        log_event(
            log, "rebuild_finished", run_id=ctx.run_id,
            stages=list(stages.executed), wall_seconds=round(wall, 3),
        )
    return EXIT_SUCCESS


def _append_metric(
    episode_root: Path, applied_command: str, stages: ReentryStages, wall: float
) -> None:
    metrics_path = episode_root / METRICS_NAME
    sequence = (
        sum(1 for line in metrics_path.read_bytes().splitlines() if line.strip()) + 1
        if metrics_path.is_file()
        else 1
    )
    metric = RebuildMetricV1(
        sequence=sequence,
        applied_command=applied_command,
        stages=stages.executed,
        rebuild_wall_clock_seconds=round(wall, 3),
        unrelated_stages_skipped=stages.skipped,
        confirmations=1,
        at=datetime.now(UTC).isoformat(),
    )
    with metrics_path.open("ab") as stream:
        stream.write(metric.model_dump_json().encode() + b"\n")


__all__ = [
    "BEYOND_STOP_STAGES",
    "CONSULTATION_FLOW_MARKER_PREFIX",
    "CONSULTATION_STOP_REASON",
    "CONSULTATION_STOP_STAGE",
    "METRICS_NAME",
    "REBUILD_LOG_NAME",
    "REENTRY_FROM_STAGES",
    "REQUIRED_STATUS",
    "RebuildMetricV1",
    "RebuildStageError",
    "assert_reservation_fresh",
    "find_reservation",
    "is_sample_flow_run",
    "reentry_stages",
    "refuse_unauthorized_full_preview",
    "run_reentry",
    "stage_compile",
    "stage_plan",
    "stage_preview",
    "stage_selection",
]


def _bundle_or_none(bundle_file: Path) -> ReviewBundle | None:
    try:
        return load_bundle(bundle_file)
    except BundleDriftError as error:
        if error.code != "bundle_unreadable":
            raise
        bootstrapped = _bootstrap_bundle(bundle_file)
        if bootstrapped is None:
            return None
        return bootstrapped
