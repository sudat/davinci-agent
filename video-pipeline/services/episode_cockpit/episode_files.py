"""Episode workspace files: brief draft, preview, flags, review chat, rebuild.

Everything in this module lives under ``episodes_root/<episode_id>/`` and
is subordinate to the StateStore: an episode must exist as a job row
before any file route resolves. Review flags are a best-effort read of a
review_command store (plan-version-bound); anything absent or unparsable
is an explicit not-yet-generated stub, never a fabricated list.
"""

# allow: SIZE_OK — the FileOps mixin (one cockpit-owned-file concern per
# method: brief/preview/flags/chat/proposals/apply/revert/rebuild); the V44-1
# multi-command delta grew apply+rebuild here rather than forking a second
# workspace mixin, the UX redesign 工程1 review-fix delta added the saved
# proposal-set record + revert relaunch bookkeeping, and the 工程2 delta adds
# the reaction outcome/linkage writers to the same mixin — split rebuild
# scheduling out when the next task touches it.

from __future__ import annotations

import logging
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from services.episode_cockpit.consultation_store import (
    canonical_policy_sha256,
    consultation_write_locked,
    latest_adopted_policy,
    policy_for_judgment,
    policy_scope_list,
)
from services.episode_cockpit.episode_ops import (
    _PIPELINE_ROOT,
    RUNNER_LOG_NAME,
    RUNNER_MODULE,
    RUNNER_STOP,
    _spawn_runner,
)
from services.episode_cockpit.errors import (
    CockpitConflictError,
    CockpitNotFoundError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.models import (
    BriefDraft,
    FrameMaterial,
    ProposalKind,
    ProposalOutcome,
    RebuildRequestEntry,
    ReviewChatEntry,
    ReviewProposalConsumed,
    ReviewReactionKind,
)
from services.episode_cockpit.preview_binding import (
    PreviewBinding,
    derive_preview_binding,
)
from services.episode_cockpit.review_apply import apply_drafts
from services.episode_cockpit.review_chat import (
    DEFAULT_LINEAGE,
    PIPELINE_STAGES,
    AppliedCommand,
    ReviewCommandDraft,
    ReviewStoreLocation,
    load_applied_command,
    plan_rebuild,
)
from services.episode_cockpit.review_frames import extract_review_frames
from services.episode_cockpit.review_interpreter import (
    NearbyContext,
    build_nearby_context,
)
from services.episode_cockpit.review_proposals import (
    REVIEW_PROPOSALS_CONSUMED_NAME,
    REVIEW_PROPOSALS_NAME,
    ReviewProposalSet,
    adoption_outcome,
    append_proposal_consumption,
    append_proposal_set,
    latest_unconsumed_set,
    load_consumed_proposals,
    load_proposal_sets,
    resolve_authoritative_drafts,
)
from services.episode_cockpit.workspace_context import WorkspaceContext
from services.foundation_io import atomic_write, canonical_model_bytes
from services.preview.render import PREVIEW_NAME
from services.review_command.events import RESTORED_EVENT_KIND
from services.review_command.restore import commit_restore
from services.review_command.store import (
    OperatorDecision0C,
    ReviewCommitError,
    load_head,
)

CHAT_LOG_NAME = "review-chat.jsonl"
REBUILD_LOG_NAME = "rebuild-requests.jsonl"
REVIEW_EVENTS_RELATIVE = ("review", "events.jsonl")
REVIEW_STORE_RELATIVE = ("review", "store")
# Mirror of services/cli/episode_runner_workspace.py constants — kept inline
# so services/ does not depend on cli/ (dependency direction guard).
_RUN_REVIEW_STORE_RELATIVE: tuple[str, ...] = ("run", "review-store")
_LOG_FILE_NAMES: frozenset[str] = frozenset({"events.jsonl", "events.jsonl.seal"})
_PLAN_CREATING_EVENT_KINDS = frozenset({"decision_applied", RESTORED_EVENT_KIND})

_LOGGER = logging.getLogger(__name__)
PREVIEW_RELATIVE = ("previews", PREVIEW_NAME)
EXECUTABLE_DOMAIN = "edit_plan"
NOT_EXECUTABLE_REASON = "command kind not rebuild-executable yet"


_SELECTION_STAGES = ("selection", "plan", "compile", "preview")


def _review_base_pin(episode_dir: Path) -> tuple[str | None, str | None]:
    try:
        head = load_head(
            episode_dir.joinpath(*REVIEW_EVENTS_RELATIVE),
            episode_dir.joinpath(*REVIEW_STORE_RELATIVE),
        )
    except (ReviewCommitError, OSError):
        return None, None
    entry = head.index.versions.get(str(head.version))
    if entry is None:
        return None, None
    return f"v{head.version}", entry.plan_sha256


def _latest_consultation_reservation(
    log_path: Path, judgment_id: str
) -> RebuildRequestEntry | None:
    try:
        lines = log_path.read_bytes().splitlines()
    except OSError:
        return None
    found: RebuildRequestEntry | None = None
    for line in lines:
        try:
            entry = RebuildRequestEntry.model_validate_json(line)
        except ValidationError:
            continue
        if (
            entry.judgment_id == judgment_id
            and not entry.spawned
            and entry.reserves_sequence is None
            and entry.failure_code is None
        ):
            found = entry
    return found


class FileOps(WorkspaceContext):
    """Reads/writes of cockpit-owned files under one episode directory."""

    def get_brief(self, episode_id: str) -> dict[str, object]:
        draft = self._load_brief(self._require_snapshot(episode_id).job.episode_id)
        return {
            "episode_id": draft.episode_id,
            "brief_text": draft.brief_text,
            "status": draft.status,
        }

    def put_brief(self, episode_id: str, *, brief_text: str) -> dict[str, object]:
        current = self._load_brief(self._require_snapshot(episode_id).job.episode_id)
        updated = BriefDraft(episode_id=current.episode_id, brief_text=brief_text)
        atomic_write(
            self._episode_dir(current.episode_id) / "brief.json",
            canonical_model_bytes(updated),
        )
        return {
            "episode_id": updated.episode_id,
            "brief_text": updated.brief_text,
            "status": updated.status,
        }

    def preview_path(self, episode_id: str) -> Path:
        episode_dir = self._require_snapshot(episode_id).job.episode_id
        path = self._episode_dir(episode_dir).joinpath(*PREVIEW_RELATIVE)
        if not path.is_file():
            raise CockpitNotFoundError("preview-not-found", f"no rendered preview at {path}")
        return path

    def preview_binding(self, episode_id: str) -> PreviewBinding | None:
        """The verified run/version binding of the served preview (None = unknown)."""

        snapshot = self._require_snapshot(episode_id)
        episode_dir = self._episode_dir(snapshot.job.episode_id)
        return derive_preview_binding(episode_dir, snapshot.stage_runs)

    def review_flags(self, episode_id: str) -> dict[str, object]:
        base = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        events_log = base.joinpath(*REVIEW_EVENTS_RELATIVE)
        plan_dir = base.joinpath(*REVIEW_STORE_RELATIVE)
        if not events_log.is_file() or not plan_dir.is_dir():
            return {"flags": [], "not_yet_generated": True}
        try:
            head = load_head(events_log, plan_dir)
        except (ReviewCommitError, OSError):
            return {"flags": [], "not_yet_generated": True}
        flags = [
            {"sequence": event.sequence, "kind": event.kind, "reason": event.reason}
            for event in head.events
            if event.kind in ("proposal_recorded", "command_deferred")
        ]
        return {"flags": flags, "not_yet_generated": False}

    def append_review_chat(  # noqa: PLR0913 (the entry fields ARE the persisted contract — one parameter per ReviewChatEntry field keeps the writer honest)
        self,
        episode_id: str,
        *,
        text: str,
        at_seconds: float | None,
        investigated: bool = False,
        hypothesis: str | None = None,
        reaction: ReviewReactionKind | None = None,
        in_response_to_set: int | None = None,
        frame_materials: tuple[FrameMaterial, ...] = (),
    ) -> dict[str, object]:
        episode_dir = self._require_snapshot(episode_id).job.episode_id
        log_path = self._episode_dir(episode_dir) / CHAT_LOG_NAME
        entry = ReviewChatEntry(
            sequence=self._next_sequence(log_path),
            text=text,
            at_seconds=at_seconds,
            investigated=investigated,
            hypothesis=hypothesis,
            reaction=reaction,
            in_response_to_set=in_response_to_set,
            frame_materials=frame_materials or None,
        )
        self._append_jsonl(log_path, entry)
        result: dict[str, object] = {"received": True, "sequence": entry.sequence}
        if investigated:
            result["investigated"] = True
            result["hypothesis"] = hypothesis
        elif hypothesis is not None:
            result["hypothesis"] = hypothesis
        if entry.reaction is not None:
            result["reaction"] = entry.reaction
        if entry.in_response_to_set is not None:
            result["in_response_to_set"] = entry.in_response_to_set
        return result

    def record_review_proposals(  # noqa: PLR0913 (the entry fields ARE the persisted contract — one parameter per saved-set field keeps the writer honest)
        self,
        episode_id: str,
        *,
        chat_sequence: int,
        drafts: tuple[ReviewCommandDraft, ...],
        responds_to_set: int | None = None,
        reaction_kind: ReviewReactionKind | None = None,
        proposal_kind: ProposalKind | None = None,
    ) -> None:
        """Persist the previewed proposal set (brief §5.3 adoption authority).

        The server — not the browser — owns the only appliable copies of
        the previewed drafts: each chat appends one
        ``cockpit-review-proposals-v1`` set carrying the plan head at
        preview time. A store that is absent or unreadable records
        ``base_plan_version=None``; such a set stays applicable only while
        the head is still the untouched bootstrap version. 工程2:
        ``responds_to_set``/``reaction_kind`` link reaction-driven sets to
        the earlier set they answer. 工程2 rework: ``proposal_kind``
        separates command bundles (all fixes applied together) from
        alternatives (one adopted); old sets without the field ARE bundles.
        """

        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        store = ReviewStoreLocation(
            log_path=episode_dir.joinpath(*REVIEW_EVENTS_RELATIVE),
            plan_dir=episode_dir.joinpath(*REVIEW_STORE_RELATIVE),
        )
        try:
            base_plan_version: str | None = (
                f"v{load_head(store.log_path, store.plan_dir).version}"
            )
        except (ReviewCommitError, OSError):
            base_plan_version = None
        proposal_log = episode_dir / REVIEW_PROPOSALS_NAME
        append_proposal_set(
            episode_dir,
            ReviewProposalSet(
                sequence=self._next_sequence(proposal_log),
                chat_sequence=chat_sequence,
                base_plan_version=base_plan_version,
                drafts=drafts,
                created_at=datetime.now(UTC).isoformat(),
                responds_to_set=responds_to_set,
                reaction_kind=reaction_kind,
                proposal_kind=proposal_kind,
            ),
        )

    def latest_unconsumed_proposal_set(self, episode_id: str) -> ReviewProposalSet | None:
        """工程2: the set a reaction message would respond to (None = nothing
        to react to)."""

        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        return latest_unconsumed_set(
            load_proposal_sets(episode_dir), load_consumed_proposals(episode_dir)
        )

    def record_proposal_outcome(
        self, episode_id: str, *, set_sequence: int, outcome: ProposalOutcome
    ) -> None:
        """Append one consumption record carrying its outcome.

        ``rejected`` requires the set to STILL be the latest unconsumed one
        (typed 409 ``proposal-not-latest`` otherwise): a rejection answers
        the proposal currently on the table, never an older one.
        """

        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        if outcome == "rejected":
            latest = latest_unconsumed_set(
                load_proposal_sets(episode_dir), load_consumed_proposals(episode_dir)
            )
            if latest is None or latest.sequence != set_sequence:
                raise CockpitConflictError(
                    "proposal-not-latest",
                    f"proposal set {set_sequence} is not the latest unconsumed "
                    "set; only the latest unconsumed set can be rejected",
                )
        consumption_log = episode_dir / REVIEW_PROPOSALS_CONSUMED_NAME
        append_proposal_consumption(
            episode_dir,
            ReviewProposalConsumed(
                sequence=self._next_sequence(consumption_log),
                set_sequence=set_sequence,
                created_at=datetime.now(UTC).isoformat(),
                outcome=outcome,
            ),
        )

    def nearby_context(self, episode_id: str, *, at_seconds: float | None) -> NearbyContext:
        """Tolerant v2-index context around the player position (task 8).

        Read-only hint for the LLM interpreter; any missing/unreadable
        index degrades to the position-only context — never an error the
        review-chat route has to surface.
        """

        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        return build_nearby_context(episode_dir, at_seconds)

    def gather_review_frames(
        self, episode_id: str, *, at_seconds: float | None, max_frames: int
    ) -> tuple[FrameMaterial, ...]:
        """工程2 rework #1: read-only stills around the player position.

        The source video (preview, else intake source) is only ever READ;
        stills land under ``<episode>/review-frames/`` as rebuildable
        runtime state. Any failure is an honest skip (empty result).
        """

        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        return extract_review_frames(episode_dir, at_seconds, max_frames=max_frames)

    def _bootstrap_review_store_if_needed(self, episode_dir: Path) -> bool:
        if (episode_dir.joinpath(*REVIEW_STORE_RELATIVE) / "versions.json").is_file():
            return False
        source_dir = episode_dir.joinpath(*_RUN_REVIEW_STORE_RELATIVE)
        if not (source_dir / "versions.json").is_file():
            return False
        log_target = episode_dir.joinpath(*REVIEW_EVENTS_RELATIVE)
        store_target = episode_dir.joinpath(*REVIEW_STORE_RELATIVE)
        store_target.mkdir(parents=True, exist_ok=True)
        log_target.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        for entry in sorted(source_dir.iterdir()):
            if not entry.is_file():
                continue
            destination = (
                log_target.parent / entry.name
                if entry.name in _LOG_FILE_NAMES
                else store_target / entry.name
            )
            shutil.copyfile(entry, destination)
            copied += 1
        _LOGGER.info("review_store_bootstrapped files=%s source=%s", copied, source_dir)
        return copied > 0

    def apply_review_command(
        self,
        episode_id: str,
        *,
        text: str,
        at_seconds: float | None,
        drafts: tuple[ReviewCommandDraft, ...] | None = None,
        sequence: int | None = None,
    ) -> dict[str, object]:
        """Apply a SAVED proposal set + rebuild plan (brief §5.3).

        Adoption authority is the server-saved proposal set persisted at
        chat time (``review-proposals.jsonl``) — never the browser's
        echoed text or a recomputed hash. ``resolve_authoritative_drafts``
        matches the request (echoed drafts field-for-field, or the
        deterministic re-derivation for the old-client ``drafts=None``
        shape) against one UNCONSUMED set whose base plan version still
        equals the current head; anything else is a typed
        proposal-not-found / proposal-mismatch (422),
        proposal-consumed (422) or proposal-stale (409). The applied set
        is then marked consumed; each command becomes its own
        AppliedCommand + plan version with ONE union rebuild plan.
        """

        snapshot = self._require_snapshot(episode_id)
        episode_dir = self._episode_dir(snapshot.job.episode_id)
        self._bootstrap_review_store_if_needed(episode_dir)
        store = ReviewStoreLocation(
            log_path=episode_dir.joinpath(*REVIEW_EVENTS_RELATIVE),
            plan_dir=episode_dir.joinpath(*REVIEW_STORE_RELATIVE),
        )
        try:
            head_version: int | None = load_head(store.log_path, store.plan_dir).version
        except (ReviewCommitError, OSError):
            head_version = None
        to_apply, applied_set = resolve_authoritative_drafts(
            text=text,
            at_seconds=at_seconds,
            drafts=drafts,
            sequence=sequence,
            sets=load_proposal_sets(episode_dir),
            consumed=load_consumed_proposals(episode_dir),
            head_version=head_version,
        )
        try:
            applied_list, plan = apply_drafts(
                list(to_apply), episode_dir=episode_dir, store=store
            )
        except ReviewCommitError as exc:
            if exc.code == "store_not_initialized":
                raise CockpitUnprocessableError(
                    "review-store-not-initialized",
                    "review store is not ready — run the pipeline to PREVIEW_READY "
                    "or ensure run/review-store exists",
                ) from exc
            raise CockpitUnprocessableError(
                exc.code.replace("_", "-"), exc.detail
            ) from exc
        self.record_proposal_outcome(
            episode_id,
            set_sequence=applied_set.sequence,
            outcome=adoption_outcome(to_apply, applied_set),
        )
        result: dict[str, object] = {
            "applied": applied_list[0].model_dump(mode="json"),
            "rebuild": plan.model_dump(mode="json"),
        }
        if len(applied_list) > 1:
            result["applied_commands"] = [
                applied.model_dump(mode="json") for applied in applied_list
            ]
        return result

    def revert_review_plan(self, episode_id: str) -> dict[str, object]:
        """Restore the previous plan version as a NEW version + rebuild.

        One apply-step per call: head vN commits vN+1 whose content equals
        vN-1's, recorded by a ``plan_restored`` event — no AppliedCommand
        and NO proposal-set consultation or consumption (a revert is not a
        command). Below the bootstrap version (head v1) there is nothing
        to revert (typed 409). Spawn-failure honesty (P1b): the restore IS
        committed, so the response reports the accurate state (200,
        ``scheduled: false`` / ``runner-start-failed``) — never a 422 that
        reads as "nothing happened". A resend whose head is still an
        UNspawned revert relaunches the SAME step (same new_version, no
        second walk-back); only a spawned revert (or another apply moving
        the head) lets the next revert walk one more step.
        """

        snapshot = self._require_snapshot(episode_id)
        episode_dir = self._episode_dir(snapshot.job.episode_id)
        self._bootstrap_review_store_if_needed(episode_dir)
        store = ReviewStoreLocation(
            log_path=episode_dir.joinpath(*REVIEW_EVENTS_RELATIVE),
            plan_dir=episode_dir.joinpath(*REVIEW_STORE_RELATIVE),
        )
        head = load_head(store.log_path, store.plan_dir)
        if head.version <= 1:
            raise CockpitConflictError(
                "nothing-to-revert",
                f"plan v{head.version} is the bootstrap version; no previous "
                "version to restore",
            )
        rebuild_log = episode_dir / REBUILD_LOG_NAME
        stages = tuple(
            stage
            for stage in PIPELINE_STAGES
            if stage in DEFAULT_LINEAGE[EXECUTABLE_DOMAIN]
        )
        creating = next(
            (
                event
                for event in reversed(head.events)
                if event.kind in _PLAN_CREATING_EVENT_KINDS
            ),
            None,
        )
        if creating is not None and creating.kind == RESTORED_EVENT_KIND:
            restored_from = int(creating.base_plan_version[1:]) - 1
            marker = f"revert-v{restored_from}"
            if not self._revert_rebuild_spawned(rebuild_log, marker):
                return self._launch_revert_rebuild(
                    episode_dir,
                    stages,
                    restored_from_version=f"v{restored_from}",
                    new_version=f"v{head.version}",
                )
        restored_from = head.version - 1
        decision = OperatorDecision0C(
            decision_id=f"dec-revert-v{head.version}",
            actor_intent="operator",
            note=f"restore plan v{restored_from} (revert of the last apply step)",
        )
        outcome = commit_restore(restored_from, decision, store.log_path, store.plan_dir)
        return self._launch_revert_rebuild(
            episode_dir,
            stages,
            restored_from_version=f"v{restored_from}",
            new_version=f"v{outcome.version}",
        )

    def _launch_revert_rebuild(
        self,
        episode_dir: Path,
        stages: tuple[str, ...],
        *,
        restored_from_version: str,
        new_version: str,
    ) -> dict[str, object]:
        """Spawn the revert runner once and record the truthful spawn state.

        The rebuild-request entry is appended only AFTER the spawn attempt
        (append-only: lines are never rewritten) so a resend can tell an
        un-launched revert from a spawned one and relaunch the same step.
        """

        marker = f"revert-{restored_from_version}"
        rebuild_log = episode_dir / REBUILD_LOG_NAME

        rebuild: dict[str, object] = {
            "stage_hint": ",".join(stages),
            "stages": list(stages),
            "runner_log": str(episode_dir / RUNNER_LOG_NAME),
            "applied_command": marker,
        }
        try:
            run_id = uuid.uuid4().hex[:12]
            _spawn_runner(
                [
                    sys.executable,
                    "-m",
                    RUNNER_MODULE,
                    "--episode-root",
                    str(episode_dir),
                    "--stop",
                    RUNNER_STOP,
                    "--from-stage",
                    stages[0],
                    "--applied-command",
                    marker,
                    "--run-id",
                    run_id,
                    "--state-store",
                    str(self._state_store_path),
                ],
                cwd=_PIPELINE_ROOT,
                log_path=episode_dir / RUNNER_LOG_NAME,
            )
        except OSError as error:
            self._append_jsonl(
                rebuild_log,
                RebuildRequestEntry(
                    sequence=self._next_sequence(rebuild_log),
                    stage_hint=",".join(stages),
                    marker=marker,
                    spawned=False,
                ),
            )
            rebuild["scheduled"] = False
            rebuild["reason"] = "runner-start-failed"
            rebuild["detail"] = str(error)
        else:
            self._append_jsonl(
                rebuild_log,
                RebuildRequestEntry(
                    sequence=self._next_sequence(rebuild_log),
                    stage_hint=",".join(stages),
                    marker=marker,
                    spawned=True,
                    run_id=run_id,
                    target_version=new_version,
                ),
            )
            rebuild["scheduled"] = True
            rebuild["run_id"] = run_id
        return {
            "restored_from_version": restored_from_version,
            "new_version": new_version,
            "rebuild": rebuild,
        }

    def _revert_rebuild_spawned(self, rebuild_log: Path, marker: str) -> bool:
        """True iff a spawned-marker entry exists for this revert marker."""

        if not rebuild_log.is_file():
            return False
        for line in rebuild_log.read_bytes().splitlines():
            try:
                entry = RebuildRequestEntry.model_validate_json(line)
            except ValidationError:
                continue
            if entry.marker == marker and entry.spawned:
                return True
        return False

    def record_rebuild(
        self,
        episode_id: str,
        *,
        stage_hint: str | None,
        applied_command: str | None = None,
        applied_commands: tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        """Record one rebuild intent; with applied command(s), schedule it (task 9).

        Deterministic path stays authoritative: the rebuild consumes the
        sealed events + plan version the apply route committed — this only
        derives the lineage stage set (``plan_rebuild``) and detaches the
        runner with a stage-subset re-entry. Only ``edit_plan``-domain
        commands (the three span-translatable kinds) are rebuild-executable
        today; the other nine kinds stay intent-only with an honest reason.
        A batch (``applied_commands``, V44-1 multi-command fix) schedules
        ONE runner over the UNION of the per-command stage sets; a batch is
        rebuild-executable only when EVERY command is.
        """

        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        log_path = episode_dir / REBUILD_LOG_NAME
        if applied_commands is not None:
            return self._record_batch_rebuild(
                episode_dir,
                log_path,
                stage_hint=stage_hint,
                applied_commands=applied_commands,
            )
        if applied_command is not None:
            applied = load_applied_command(episode_dir, applied_command)
            plan = plan_rebuild(applied, DEFAULT_LINEAGE)
            return self._schedule_rebuild(
                episode_dir,
                log_path,
                stage_hint=stage_hint,
                applied=[applied],
                stages=tuple(plan.stages),
            )
        entry = RebuildRequestEntry(
            sequence=self._next_sequence(log_path), stage_hint=stage_hint
        )
        self._append_jsonl(log_path, entry)
        return {
            "stage_hint": entry.stage_hint,
            "scheduled": False,
            "note": "rebuild scheduling is not implemented yet; intent recorded",
        }

    def _record_batch_rebuild(
        self,
        episode_dir: Path,
        log_path: Path,
        *,
        stage_hint: str | None,
        applied_commands: tuple[str, ...],
    ) -> dict[str, object]:
        if not applied_commands:
            raise CockpitUnprocessableError(
                "applied-commands-empty", "applied_commands must name at least one command"
            )
        applied = [
            load_applied_command(episode_dir, command_id)
            for command_id in applied_commands
        ]
        stage_sets = [set(plan_rebuild(command, DEFAULT_LINEAGE).stages) for command in applied]
        union = tuple(
            stage for stage in PIPELINE_STAGES if any(stage in s for s in stage_sets)
        )
        return self._schedule_rebuild(
            episode_dir,
            log_path,
            stage_hint=stage_hint,
            applied=applied,
            stages=union,
        )

    def _schedule_rebuild(
        self,
        episode_dir: Path,
        log_path: Path,
        *,
        stage_hint: str | None,
        applied: list[AppliedCommand],
        stages: tuple[str, ...],
    ) -> dict[str, object]:
        resolved_hint = stage_hint if stage_hint is not None else ",".join(stages)
        target_version = next(
            (command.result_plan_version for command in reversed(applied)
             if command.result_plan_version is not None),
            None,
        )
        entry = RebuildRequestEntry(
            sequence=self._next_sequence(log_path),
            stage_hint=resolved_hint,
            target_version=target_version,
        )
        self._append_jsonl(log_path, entry)  # the 予約 (pre-spawn reservation)
        primary = applied[0]
        result: dict[str, object]
        if any(command.affected_domain != EXECUTABLE_DOMAIN for command in applied):
            result = {
                "stage_hint": resolved_hint,
                "scheduled": False,
                "reason": NOT_EXECUTABLE_REASON,
                "applied_command": primary.command_id,
                "rebuild_stages": list(stages),
            }
        else:
            run_id = uuid.uuid4().hex[:12]
            try:
                _spawn_runner(
                    [
                        sys.executable,
                        "-m",
                        RUNNER_MODULE,
                        "--episode-root",
                        str(episode_dir),
                        "--stop",
                        RUNNER_STOP,
                        "--from-stage",
                        stages[0],
                        "--applied-command",
                        primary.command_id,
                        "--run-id",
                        run_id,
                        "--state-store",
                        str(self._state_store_path),
                    ],
                    cwd=_PIPELINE_ROOT,
                    log_path=episode_dir / RUNNER_LOG_NAME,
                )
            except OSError as error:
                raise CockpitUnprocessableError(
                    "runner-spawn-failed", f"cannot start the rebuild runner: {error}"
                ) from error
            self._append_jsonl(
                log_path,
                RebuildRequestEntry(
                    sequence=self._next_sequence(log_path),
                    stage_hint=resolved_hint,
                    spawned=True,
                    run_id=run_id,
                    target_version=target_version,
                    reserves_sequence=entry.sequence,
                ),
            )
            result = {
                "stage_hint": resolved_hint,
                "scheduled": True,
                "stages": list(stages),
                "runner_log": str(episode_dir / RUNNER_LOG_NAME),
                "applied_command": primary.command_id,
                "run_id": run_id,
            }
        if len(applied) > 1:
            result["applied_commands"] = [command.command_id for command in applied]
        return result

    def record_consultation_rebuild(
        self, episode_id: str, *, judgment_id: str
    ) -> dict[str, object]:
        """Schedule a selection rebuild for an adopted consultation judgment.

        The reservation pins the judgment's scope, policy bytes, and
        review-store base so the runner refuses a stale run instead of
        silently using the latest policy (the 予約→起動→成果 discipline).
        The lineage is the selection re-entry projection
        (selection→plan→compile→preview). The rebuild consumes NO
        consultation LLM budget (deterministic orchestration + the chain's
        own director path). ``runner-active`` (a runner holds the lock)
        propagates as the typed 409 — the judgment route maps it to an
        unscheduled 200 with the recorded judgment intact. A reservation
        for the same judgment is reused without a new row or spawn; a
        spawn failure appends a terminal failed row (never an orphan
        ``requested``) and raises.
        """

        snapshot = self._require_snapshot(episode_id)
        episode_dir = self._episode_dir(snapshot.job.episode_id)
        if snapshot.job.status == "FROZEN":
            raise CockpitUnprocessableError(
                "job-frozen",
                "この動画は確定済みのため、作り直しを行いませんでした。",
            )
        if snapshot.job.status != "PREVIEW_READY":
            raise CockpitUnprocessableError(
                "episode-not-preview-ready",
                "selection rebuild needs a PREVIEW_READY job; "
                f"job is at {snapshot.job.status}",
            )
        policy = policy_for_judgment(episode_dir, judgment_id)
        latest = latest_adopted_policy(episode_dir)
        if latest is None or latest.judgment_id != judgment_id:
            raise CockpitUnprocessableError(
                "reserved-policy-changed",
                "予約した判断が変わりました。古い判断の編集は確定していません。",
            )
        log_path = episode_dir / REBUILD_LOG_NAME
        with consultation_write_locked(episode_dir):
            existing = _latest_consultation_reservation(log_path, judgment_id)
            if existing is not None:
                return {
                    "stage_hint": existing.stage_hint,
                    "scheduled": bool(existing.run_id),
                    "stages": list(_SELECTION_STAGES),
                    "judgment_id": judgment_id,
                    "reservation_sequence": existing.sequence,
                    "reused": True,
                }
            start = PIPELINE_STAGES.index("selection")
            stop = PIPELINE_STAGES.index("preview")
            stages = tuple(PIPELINE_STAGES[start : stop + 1])
            marker = f"consultation-{judgment_id}"
            base_version, base_sha = _review_base_pin(episode_dir)
            reservation = RebuildRequestEntry(
                sequence=self._next_sequence(log_path),
                stage_hint=",".join(stages),
                judgment_id=judgment_id,
                policy_scope=tuple(policy_scope_list(policy)),
                policy_sha256=canonical_policy_sha256(policy),
                base_plan_version=base_version,
                base_plan_sha256=base_sha,
            )
            self._append_jsonl(log_path, reservation)  # the 予約 (pre-spawn reservation)
        run_id = uuid.uuid4().hex[:12]
        try:
            _spawn_runner(
                [
                    sys.executable,
                    "-m",
                    RUNNER_MODULE,
                    "--episode-root",
                    str(episode_dir),
                    "--stop",
                    RUNNER_STOP,
                    "--from-stage",
                    stages[0],
                    "--applied-command",
                    marker,
                    "--reservation-sequence",
                    str(reservation.sequence),
                    "--run-id",
                    run_id,
                    "--state-store",
                    str(self._state_store_path),
                ],
                cwd=_PIPELINE_ROOT,
                log_path=episode_dir / RUNNER_LOG_NAME,
            )
        except CockpitConflictError:
            self._append_jsonl(
                log_path,
                RebuildRequestEntry(
                    sequence=self._next_sequence(log_path),
                    stage_hint=",".join(stages),
                    judgment_id=judgment_id,
                    reserves_sequence=reservation.sequence,
                    failure_code="runner-active",
                    detail="実行中の処理があるため、作り直しを始めませんでした。",
                ),
            )
            raise
        except OSError as error:
            self._append_jsonl(
                log_path,
                RebuildRequestEntry(
                    sequence=self._next_sequence(log_path),
                    stage_hint=",".join(stages),
                    judgment_id=judgment_id,
                    reserves_sequence=reservation.sequence,
                    failure_code="runner-spawn-failed",
                    detail="再生成の起動に失敗しました。相談へ戻ってやり直せます。",
                ),
            )
            raise CockpitUnprocessableError(
                "runner-spawn-failed", f"cannot start the rebuild runner: {error}"
            ) from error
        self._append_jsonl(
            log_path,
            RebuildRequestEntry(
                sequence=self._next_sequence(log_path),
                stage_hint=",".join(stages),
                spawned=True,
                run_id=run_id,
                reserves_sequence=reservation.sequence,
                judgment_id=judgment_id,
            ),
        )
        return {
            "stage_hint": ",".join(stages),
            "scheduled": True,
            "stages": list(stages),
            "runner_log": str(episode_dir / RUNNER_LOG_NAME),
            "applied_command": marker,
            "run_id": run_id,
            "judgment_id": judgment_id,
            "reservation_sequence": reservation.sequence,
        }

    def _load_brief(self, episode_id: str) -> BriefDraft:
        path = self._episode_dir(episode_id) / "brief.json"
        try:
            draft = BriefDraft.model_validate_json(path.read_bytes())
        except OSError as error:
            raise CockpitNotFoundError(
                "brief-not-found", f"no brief draft for episode {episode_id}"
            ) from error
        except ValidationError as error:
            raise CockpitConflictError(
                "brief-not-draft",
                f"episode {episode_id} brief is not an editable draft: {error}",
            ) from error
        return draft

    def _next_sequence(self, log_path: Path) -> int:
        if not log_path.is_file():
            return 1
        return sum(1 for line in log_path.read_bytes().splitlines() if line.strip()) + 1

    def _append_jsonl(
        self, log_path: Path, entry: ReviewChatEntry | RebuildRequestEntry
    ) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as stream:
            stream.write(canonical_model_bytes(entry) + b"\n")


__all__ = ["FileOps"]
