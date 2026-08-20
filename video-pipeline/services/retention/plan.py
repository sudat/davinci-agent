"""Plan phase: state-aware decisions for every managed path, no deletion.

The planner walks each job directory no-follow, applies investigation
holds and the FROZEN-plus-period eligibility rule, and reconciles every
sweepable file against the sealed registry before proposing deletion.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import canonical_model_bytes, sha256_file
from services.retention.audit import DeletionAudit
from services.retention.errors import RetentionError
from services.retention.policy import classify_name, hold_reason, sweep_unlocked
from services.retention.reconcile import reconcile
from services.retention.records import AuditReason, Decision
from services.retention.walk import is_within, load_job_state, load_registry, walk_tree

if TYPE_CHECKING:
    from services.retention.models import (
        JobRetentionState,
        RetentionPolicy,
        RetentionRegistry,
    )


def wall_clock() -> int:
    return int(time.time())


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    run_id: str
    decisions: tuple[Decision, ...]
    jobs_seen: int


@dataclass(frozen=True, slots=True)
class JobContext:
    """Per-job planning inputs: identity, state, registry, root, and clock."""

    job_id: str
    state: JobRetentionState | None
    registry: RetentionRegistry | None
    root_real: str
    now: int


class Planner:
    """Builds the full decision list for one jobs root under one policy."""

    def __init__(
        self, policy: RetentionPolicy, clock: Callable[[], int] = wall_clock
    ) -> None:
        self._policy = policy
        self._clock = clock

    def plan(self, jobs_root: Path) -> CollectionPlan:
        root_real = self._require_root(jobs_root)
        now = self._clock()
        audit_lines = len(DeletionAudit(jobs_root).records())
        policy_sha = hashlib.sha256(canonical_model_bytes(self._policy)).hexdigest()
        run_id = hashlib.sha256(
            f"{root_real}|{policy_sha}|{now}|{audit_lines}".encode()
        ).hexdigest()
        decisions: list[Decision] = []
        jobs_seen = 0
        for job_dir in self._job_dirs(jobs_root):
            jobs_seen += 1
            context = JobContext(
                job_id=job_dir.name,
                state=load_job_state(job_dir),
                registry=load_registry(job_dir),
                root_real=root_real,
                now=now,
            )
            decisions.extend(self._plan_job(job_dir, context))
        return CollectionPlan(
            run_id=run_id, decisions=tuple(decisions), jobs_seen=jobs_seen
        )

    def _require_root(self, jobs_root: Path) -> str:
        if not jobs_root.is_dir():
            raise RetentionError(
                "jobs-root-missing", f"jobs root is not a directory: {jobs_root}"
            )
        return os.path.realpath(jobs_root)

    def _job_dirs(self, jobs_root: Path) -> list[Path]:
        with os.scandir(jobs_root) as scan:
            return sorted(
                (
                    Path(entry.path)
                    for entry in scan
                    if entry.is_dir(follow_symlinks=False) and not entry.is_symlink()
                ),
                key=lambda path: path.name,
            )

    def _plan_job(self, job_dir: Path, context: JobContext) -> list[Decision]:
        decisions: list[Decision] = []
        with os.scandir(job_dir) as scan:
            top_entries = sorted(scan, key=lambda item: item.name)
        for entry in top_entries:
            top_path = f"{context.job_id}/{entry.name}"
            if entry.is_symlink():
                decisions.append(
                    symlink_decision(
                        context.job_id, top_path, Path(entry.path), context.root_real
                    )
                )
                continue
            name_class = classify_name(entry.name, self._policy)
            if name_class in ("rebuildable", "runtime-cache"):
                decisions.extend(
                    self._plan_sweepable(context, entry.name, Path(entry.path))
                )
                continue
            decisions.append(
                Decision(
                    job_id=context.job_id,
                    path=top_path,
                    action="retained",
                    reason=retain_reason(name_class),
                )
            )
        return decisions

    def _plan_sweepable(
        self, context: JobContext, top_name: str, top_path: Path
    ) -> list[Decision]:
        job_id = context.job_id
        top_rel = f"{job_id}/{top_name}"
        hold = hold_reason(context.state)
        if hold is not None:
            return [
                Decision(job_id=job_id, path=top_rel, action="retained", reason=hold)
            ]
        if not sweep_unlocked(context.state, self._policy, context.now):
            return [
                Decision(
                    job_id=job_id,
                    path=top_rel,
                    action="retained",
                    reason="retention-period",
                )
            ]
        if not top_path.is_dir():
            return [file_decision(job_id, top_rel, top_name, top_path, context)]
        decisions: list[Decision] = []
        for walked in walk_tree(top_path, context.root_real):
            decision_path = f"{top_rel}/{walked.relative}"
            registry_key = f"{top_name}/{walked.relative}"
            absolute = top_path / walked.relative
            if walked.kind == "symlink":
                decisions.append(
                    symlink_decision(job_id, decision_path, absolute, context.root_real)
                )
            elif walked.kind == "file":
                decisions.append(
                    file_decision(job_id, decision_path, registry_key, absolute, context)
                )
        return decisions


def retain_reason(name_class: str) -> AuditReason:
    if name_class == "protected":
        return "protected"
    if name_class == "authoritative":
        return "authoritative"
    if name_class == "manual-finalization":
        return "manual-finalization"
    return "unclassified"


def file_decision(
    job_id: str,
    decision_path: str,
    registry_key: str,
    absolute: Path,
    context: JobContext,
) -> Decision:
    try:
        disk_sha = sha256_file(absolute)
        size = absolute.stat().st_size
    except OSError as error:
        raise RetentionError("io-error", f"cannot hash {absolute}: {error}") from error
    verdict = reconcile(context.registry, registry_key, disk_sha)
    if verdict == "verified":
        return Decision(
            job_id=job_id,
            path=decision_path,
            action="planned_delete",
            reason="eligible",
            disk_sha256=disk_sha,
            size_bytes=size,
            registry_sha256=disk_sha,
        )
    reason: AuditReason = (
        "unregistered" if verdict == "unregistered" else "hash-mismatch"
    )
    registered = (
        None if context.registry is None else context.registry.entries.get(registry_key)
    )
    return Decision(
        job_id=job_id,
        path=decision_path,
        action="retained",
        reason=reason,
        disk_sha256=disk_sha,
        size_bytes=size,
        registry_sha256=None if registered is None else registered.sha256,
    )


def symlink_decision(
    job_id: str, decision_path: str, link_path: Path, root_real: str
) -> Decision:
    link_target = link_path.readlink()
    real = os.path.realpath(link_path)
    reason: AuditReason = (
        "symlink-escape" if not is_within(real, root_real) else "symlink"
    )
    return Decision(
        job_id=job_id,
        path=decision_path,
        action="retained",
        reason=reason,
        symlink_target=str(link_target),
    )


__all__ = ["CollectionPlan", "JobContext", "Planner", "wall_clock"]
