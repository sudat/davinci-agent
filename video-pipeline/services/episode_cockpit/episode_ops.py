"""Job-state operations: intake, listing, status, publish-status read path.

Episode identity is deterministic (``ep-`` + sha256 of the resolved source
folder path), one job row per episode, and the job-runner StateStore is
the ONLY authority — listing reads the same schema through a read-only
SQLite connection rather than a cockpit-side index. Task 47 adds the
applied-review-command rebuild resolution read (lineage-derived stage set).
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from services.episode_cockpit.errors import (
    CockpitConflictError,
    CockpitUnprocessableError,
)
from services.episode_cockpit.models import BriefDraft
from services.episode_cockpit.review_chat import (
    DEFAULT_LINEAGE,
    RebuildPlan,
    load_applied_command,
    plan_rebuild,
)
from services.episode_cockpit.workspace_context import WorkspaceContext
from services.foundation_io import atomic_write, canonical_model_bytes
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_store import StateStore
from services.publish.idempotency import UploadLedger, UploadLedgerError
from services.publish.models import PublishPackageV1

INTAKE_STAGE = "intake"
BRIEF_NAME = "brief.json"
PUBLISH_PACKAGE_RELATIVE = ("publish", "package.json")


class JobOps(WorkspaceContext):
    """Episode lifecycle reads/writes against the existing StateStore."""

    def create_episode(self, *, source_folder: str, brief_text: str) -> dict[str, object]:
        folder = Path(source_folder)
        if not folder.is_dir():
            raise CockpitUnprocessableError(
                "source-folder-not-found", f"source folder does not exist: {folder}"
            )
        episode_id = "ep-" + hashlib.sha256(str(folder.resolve()).encode()).hexdigest()[:16]
        try:
            with StateStore.open(self._state_store_path) as store:
                store.create_job(
                    job_id=episode_id, episode_id=episode_id, current_stage=INTAKE_STAGE
                )
        except StateStoreError as error:
            if error.code == "job-exists":
                raise CockpitConflictError(
                    "episode-exists",
                    f"an episode for source folder {folder} already exists ({episode_id})",
                ) from error
            raise
        self._episode_dir(episode_id).mkdir(parents=True, exist_ok=True)
        atomic_write(
            self._episode_dir(episode_id) / BRIEF_NAME,
            canonical_model_bytes(BriefDraft(episode_id=episode_id, brief_text=brief_text)),
        )
        return {
            "episode_id": episode_id,
            "job_id": episode_id,
            "status": "CREATED",
            "brief_status": "draft",
        }

    def list_episodes(self) -> dict[str, object]:
        return {"episodes": self._list_job_rows()}

    def episode_status(self, episode_id: str) -> dict[str, object]:
        snapshot = self._require_snapshot(episode_id)
        job = snapshot.job
        return {
            "episode_id": job.episode_id,
            "job_id": job.job_id,
            "status": job.status,
            "current_stage": job.current_stage,
            "created_at_seq": job.created_at_seq,
            "updated_at_seq": job.updated_at_seq,
            "stage_runs": [
                {
                    "stage_name": run.stage_name,
                    "status": run.status,
                    "retry_count": run.retry_count,
                    "last_error_code": run.last_error_code,
                }
                for run in snapshot.stage_runs
            ],
        }

    def publish_status(self, episode_id: str) -> dict[str, object]:
        """Read-only publish surface: package file + upload ledger, never a write."""
        episode_dir = self._episode_dir(self._require_snapshot(episode_id).job.episode_id)
        package_path = episode_dir.joinpath(*PUBLISH_PACKAGE_RELATIVE)
        if not package_path.is_file():
            return {"available": False, "reason": "publish-package-not-built"}
        try:
            package = PublishPackageV1.model_validate_json(package_path.read_bytes())
            ledger_record = UploadLedger(
                package_path.parent, package.channel_target
            ).lookup(package.idempotency_key)
        except (OSError, ValidationError, UploadLedgerError) as error:
            raise CockpitUnprocessableError("publish-state-unreadable", str(error)) from error
        upload: dict[str, object] | None = None
        remote_link: str | None = None
        if ledger_record is not None:
            upload = {
                "status": ledger_record.status,
                "video_id": ledger_record.video_id,
                "reason": ledger_record.reason,
            }
            if ledger_record.status == "completed" and ledger_record.video_id:
                remote_link = f"https://youtu.be/{ledger_record.video_id}"
        return {
            "available": True,
            "package": {
                "selected_title": package.selected_title,
                "title_candidates": list(package.title_candidates),
                "visibility": package.visibility,
                "channel_target": package.channel_target,
                "idempotency_key": package.idempotency_key,
                "schedule_time": package.schedule_time,
                "remote_video_id": package.remote_video_id,
            },
            "upload": upload,
            "remote_link": remote_link,
        }

    def resolve_rebuild_stages(self, episode_id: str, applied_command: str) -> RebuildPlan:
        """Resolve an applied review command to its minimal rebuild stage set (task 47).

        Read-only against the episode's applied-command audit log; scheduling
        stays out of scope (the caller records the intent via record_rebuild).
        """

        snapshot = self._require_snapshot(episode_id)
        episode_dir = self._episode_dir(snapshot.job.episode_id)
        applied = load_applied_command(episode_dir, applied_command)
        return plan_rebuild(applied, DEFAULT_LINEAGE)

    def _list_job_rows(self) -> list[dict[str, object]]:
        if not self._state_store_path.is_file():
            return []
        uri = f"{self._state_store_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute(
                "SELECT job_id, episode_id, current_stage, status, created_at_seq,"
                " updated_at_seq FROM jobs ORDER BY created_at_seq"
            ).fetchall()
        except sqlite3.DatabaseError as error:
            raise CockpitUnprocessableError("state-store-unreadable", str(error)) from error
        finally:
            connection.close()
        return [
            {
                "episode_id": row[1],
                "job_id": row[0],
                "status": row[3],
                "current_stage": row[2],
                "created_at_seq": row[4],
                "updated_at_seq": row[5],
            }
            for row in rows
        ]


__all__ = [
    "BRIEF_NAME",
    "INTAKE_STAGE",
    "PUBLISH_PACKAGE_RELATIVE",
    "JobOps",
]
