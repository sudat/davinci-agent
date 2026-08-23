"""Episode workspace files: brief draft, preview, flags, review chat, rebuild.

Everything in this module lives under ``episodes_root/<episode_id>/`` and
is subordinate to the StateStore: an episode must exist as a job row
before any file route resolves. Review flags are a best-effort read of a
review_command store (plan-version-bound); anything absent or unparsable
is an explicit not-yet-generated stub, never a fabricated list.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError

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
    RebuildRequestEntry,
    ReviewChatEntry,
)
from services.episode_cockpit.review_chat import (
    DEFAULT_LINEAGE,
    ReviewChatContext,
    ReviewStoreLocation,
    apply_command,
    interpret_command,
    load_applied_command,
    plan_rebuild,
    record_applied_command,
)
from services.episode_cockpit.review_interpreter import (
    NearbyContext,
    build_nearby_context,
)
from services.episode_cockpit.workspace_context import WorkspaceContext
from services.foundation_io import atomic_write, canonical_model_bytes
from services.preview.render import PREVIEW_NAME
from services.review_command.store import ReviewCommitError, load_head

CHAT_LOG_NAME = "review-chat.jsonl"
REBUILD_LOG_NAME = "rebuild-requests.jsonl"
REVIEW_EVENTS_RELATIVE = ("review", "events.jsonl")
REVIEW_STORE_RELATIVE = ("review", "store")
PREVIEW_RELATIVE = ("previews", PREVIEW_NAME)
EXECUTABLE_DOMAIN = "edit_plan"
NOT_EXECUTABLE_REASON = "command kind not rebuild-executable yet"


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

    def append_review_chat(
        self, episode_id: str, *, text: str, at_seconds: float | None
    ) -> dict[str, object]:
        episode_dir = self._require_snapshot(episode_id).job.episode_id
        log_path = self._episode_dir(episode_dir) / CHAT_LOG_NAME
        entry = ReviewChatEntry(
            sequence=self._next_sequence(log_path), text=text, at_seconds=at_seconds
        )
        self._append_jsonl(log_path, entry)
        return {"received": True, "sequence": entry.sequence}

    def nearby_context(self, episode_id: str, *, at_seconds: float | None) -> NearbyContext:
        """Tolerant v2-index context around the player position (task 8).

        Read-only hint for the LLM interpreter; any missing/unreadable
        index degrades to the position-only context — never an error the
        review-chat route has to surface.
        """

        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        return build_nearby_context(episode_dir, at_seconds)

    def apply_review_command(
        self, episode_id: str, *, text: str, at_seconds: float | None
    ) -> dict[str, object]:
        """Task 51 wiring: apply one NL correction and derive its rebuild plan.

        Interpretation is deterministic, so re-interpreting the same
        (text, at_seconds) reproduces the echoed draft's command exactly;
        the applied command lands in the episode audit log and the
        lineage-scoped stage set comes back for the rebuild indicator.
        """

        snapshot = self._require_snapshot(episode_id)
        episode_dir = self._episode_dir(snapshot.job.episode_id)
        draft = interpret_command(text, ReviewChatContext(at_seconds=at_seconds))
        applied = apply_command(
            draft,
            store=ReviewStoreLocation(
                log_path=episode_dir.joinpath(*REVIEW_EVENTS_RELATIVE),
                plan_dir=episode_dir.joinpath(*REVIEW_STORE_RELATIVE),
            ),
        )
        record_applied_command(episode_dir, applied)
        plan = plan_rebuild(applied, DEFAULT_LINEAGE)
        return {
            "applied": applied.model_dump(mode="json"),
            "rebuild": plan.model_dump(mode="json"),
        }

    def record_rebuild(
        self, episode_id: str, *, stage_hint: str | None, applied_command: str | None = None
    ) -> dict[str, object]:
        """Record one rebuild intent; with an applied command, schedule it (task 9).

        Deterministic path stays authoritative: the rebuild consumes the
        sealed events + plan version the apply route committed — this only
        derives the lineage stage set (``plan_rebuild``) and detaches the
        runner with a stage-subset re-entry. Only ``edit_plan``-domain
        commands (the three span-translatable kinds) are rebuild-executable
        today; the other nine kinds stay intent-only with an honest reason.
        """

        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        log_path = episode_dir / REBUILD_LOG_NAME
        if applied_command is None:
            entry = RebuildRequestEntry(
                sequence=self._next_sequence(log_path), stage_hint=stage_hint
            )
            self._append_jsonl(log_path, entry)
            return {
                "stage_hint": entry.stage_hint,
                "scheduled": False,
                "note": "rebuild scheduling is not implemented yet; intent recorded",
            }
        applied = load_applied_command(episode_dir, applied_command)
        plan = plan_rebuild(applied, DEFAULT_LINEAGE)
        resolved_hint = stage_hint if stage_hint is not None else ",".join(plan.stages)
        entry = RebuildRequestEntry(
            sequence=self._next_sequence(log_path), stage_hint=resolved_hint
        )
        self._append_jsonl(log_path, entry)
        if applied.affected_domain != EXECUTABLE_DOMAIN:
            return {
                "stage_hint": resolved_hint,
                "scheduled": False,
                "reason": NOT_EXECUTABLE_REASON,
                "applied_command": applied.command_id,
                "rebuild_stages": list(plan.stages),
            }
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
                    plan.stages[0],
                    "--applied-command",
                    applied.command_id,
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
        return {
            "stage_hint": resolved_hint,
            "scheduled": True,
            "stages": list(plan.stages),
            "runner_log": str(episode_dir / RUNNER_LOG_NAME),
            "applied_command": applied.command_id,
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
