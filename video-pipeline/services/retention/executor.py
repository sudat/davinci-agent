"""Execute phase: apply a plan with re-verification before every unlink.

Nothing is ever deleted unless it appears as a ``planned_delete`` in the
supplied plan; each target is re-hashed immediately before unlink (plan
vs disk drift retains instead of deleting) and re-stated after unlink so
the audit can only record disk truth. Empty directories left under a
sweepable name are pruned bottom-up and audited.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import sha256_file
from services.retention.audit import DeletionAudit
from services.retention.errors import RetentionError
from services.retention.records import Decision, GcSummary
from services.retention.walk import is_within, walk_tree

if TYPE_CHECKING:
    from services.retention.models import RetentionPolicy
    from services.retention.plan import CollectionPlan

AUDIT_FILE_NAME = "gc-audit.jsonl"


@dataclass(frozen=True, slots=True)
class GcOutcome:
    plan: CollectionPlan
    summary: GcSummary


@dataclass(slots=True)
class _TouchLog:
    sweepable_tops: dict[str, set[str]] = field(default_factory=dict)


class Executor:
    """Applies a :class:`~services.retention.plan.CollectionPlan` to disk."""

    def __init__(
        self,
        policy: RetentionPolicy,
        clock: Callable[[], int],
        root_real: str,
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._root_real = root_real

    def execute(self, jobs_root: Path, plan: CollectionPlan) -> GcOutcome:
        now = self._clock()
        final: list[Decision] = []
        touched = _TouchLog()
        for decision in plan.decisions:
            if decision.action != "planned_delete":
                final.append(decision)
                continue
            final.append(self._execute_decision(jobs_root, decision, touched))
        pruned = self._prune_empty_dirs(jobs_root, touched)
        records = tuple(
            item.audit_record(run_id=plan.run_id, mode="execute", recorded_epoch_s=now)
            for item in (*final, *pruned)
        )
        DeletionAudit(jobs_root).append(records)
        summary = GcSummary(
            mode="execute",
            run_id=plan.run_id,
            jobs_seen=plan.jobs_seen,
            planned_deletes=sum(
                1 for item in plan.decisions if item.action == "planned_delete"
            ),
            deleted=sum(
                1
                for record in records
                if record.decision == "deleted" and record.reason == "eligible"
            ),
            retained=sum(1 for record in records if record.decision == "retained"),
            pruned_dirs=len(pruned),
            audit_path=AUDIT_FILE_NAME,
            now_epoch_s=now,
        )
        return GcOutcome(plan=plan, summary=summary)

    def _execute_decision(
        self, jobs_root: Path, decision: Decision, touched: _TouchLog
    ) -> Decision:
        absolute = jobs_root / decision.path
        parent_real = os.path.realpath(absolute.parent)
        if not is_within(parent_real, self._root_real):
            raise RetentionError(
                "escape-refused",
                f"planned path escapes the managed root: {decision.path}",
            )
        try:
            disk_sha = sha256_file(absolute)
        except OSError:
            return self._drifted(decision)
        if disk_sha != decision.disk_sha256:
            return self._drifted(decision)
        try:
            absolute.unlink()
        except OSError as error:
            raise RetentionError(
                "delete-failed", f"cannot delete {absolute}: {error}"
            ) from error
        if absolute.exists():
            raise RetentionError(
                "delete-failed", f"unlink reported success but path remains: {absolute}"
            )
        job_id, top_name = decision.path.split("/", maxsplit=2)[:2]
        touched.sweepable_tops.setdefault(job_id, set()).add(top_name)
        return decision.model_copy(update={"action": "deleted"})

    def _drifted(self, decision: Decision) -> Decision:
        return decision.model_copy(
            update={"action": "retained", "reason": "drift-before-execute"}
        )

    def _prune_empty_dirs(self, jobs_root: Path, touched: _TouchLog) -> list[Decision]:
        pruned: list[Decision] = []
        for job_id in sorted(touched.sweepable_tops):
            for top_name in sorted(touched.sweepable_tops[job_id]):
                pruned.extend(
                    self._prune_top(jobs_root / job_id / top_name, job_id, top_name)
                )
        return pruned

    def _prune_top(self, top_dir: Path, job_id: str, top_name: str) -> list[Decision]:
        if not top_dir.is_dir():
            return []
        directories = sorted(
            (entry for entry in walk_tree(top_dir, self._root_real) if entry.kind == "dir"),
            key=lambda entry: entry.relative.count("/"),
            reverse=True,
        )
        pruned: list[Decision] = []
        for walked in directories:
            pruned.extend(
                self._rmdir_if_empty(
                    job_id,
                    f"{job_id}/{top_name}/{walked.relative}",
                    top_dir / walked.relative,
                )
            )
        pruned.extend(self._rmdir_if_empty(job_id, f"{job_id}/{top_name}", top_dir))
        return pruned

    def _rmdir_if_empty(
        self, job_id: str, decision_path: str, directory: Path
    ) -> list[Decision]:
        try:
            directory.rmdir()
        except OSError:
            return []
        return [
            Decision(
                job_id=job_id,
                path=decision_path,
                action="deleted",
                reason="empty-dir-prune",
            )
        ]


__all__ = ["AUDIT_FILE_NAME", "Executor", "GcOutcome"]
