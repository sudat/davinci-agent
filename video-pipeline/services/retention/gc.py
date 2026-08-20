"""Retention GC facade and CLI: plan-first, verify-before-delete, audited.

``python -m services.retention.gc --jobs-root DIR --policy FILE`` defaults
to DRY-RUN (full decision list, nothing deleted); ``--execute`` applies a
freshly computed plan through the Executor, which re-hashes every target
immediately before unlink so the audit can only ever reflect disk truth.
The managed unit is a real directory directly under the jobs root;
everything else at the root (like the append-only ``gc-audit.jsonl``) is
never a candidate.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from pydantic import ValidationError

from services.retention.audit import DeletionAudit
from services.retention.errors import RetentionError
from services.retention.executor import AUDIT_FILE_NAME, Executor, GcOutcome
from services.retention.plan import CollectionPlan, Planner, wall_clock
from services.retention.policy import load_policy
from services.retention.records import GcSummary, RunMode

if TYPE_CHECKING:
    from services.retention.models import RetentionPolicy


class RetentionGc:
    """Plan/execute garbage collection for one jobs root under one policy."""

    def __init__(
        self, policy: RetentionPolicy, clock: Callable[[], int] = wall_clock
    ) -> None:
        self._policy = policy
        self._clock = clock

    def plan(self, jobs_root: Path) -> CollectionPlan:
        return Planner(self._policy, self._clock).plan(jobs_root)

    def execute(self, jobs_root: Path, plan: CollectionPlan) -> GcOutcome:
        root_real = os.path.realpath(jobs_root)
        return Executor(self._policy, self._clock, root_real).execute(jobs_root, plan)

    def run(self, jobs_root: Path, mode: RunMode) -> GcOutcome:
        plan = self.plan(jobs_root)
        if mode == "execute":
            return self.execute(jobs_root, plan)
        now = self._clock()
        records = tuple(
            decision.audit_record(run_id=plan.run_id, mode="dry_run", recorded_epoch_s=now)
            for decision in plan.decisions
        )
        DeletionAudit(jobs_root).append(records)
        summary = GcSummary(
            mode="dry_run",
            run_id=plan.run_id,
            jobs_seen=plan.jobs_seen,
            planned_deletes=sum(
                1 for decision in plan.decisions if decision.action == "planned_delete"
            ),
            deleted=0,
            retained=sum(1 for record in records if record.decision == "retained"),
            pruned_dirs=0,
            audit_path=AUDIT_FILE_NAME,
            now_epoch_s=now,
        )
        return GcOutcome(plan=plan, summary=summary)


class _CliParser(argparse.ArgumentParser):
    """Argparse that raises a typed error instead of SystemExit."""

    def error(self, message: str) -> NoReturn:
        raise RetentionError("cli-arguments", message)


def _parser() -> argparse.ArgumentParser:
    parser = _CliParser(prog="services.retention.gc")
    parser.add_argument("--jobs-root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--now-epoch", type=int, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        clock: Callable[[], int] = (
            wall_clock
            if arguments.now_epoch is None
            else (lambda: int(arguments.now_epoch))
        )
        policy = load_policy(arguments.policy)
        mode: RunMode = "execute" if arguments.execute else "dry_run"
        outcome = RetentionGc(policy, clock).run(arguments.jobs_root, mode)
    except (RetentionError, ValidationError) as error:
        print(error, file=sys.stderr)
        return 2
    summary = outcome.summary
    print(
        f"retention-gc mode={summary.mode} jobs={summary.jobs_seen} "
        f"deleted={summary.deleted} planned={summary.planned_deletes} "
        f"retained={summary.retained} audit={summary.audit_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
