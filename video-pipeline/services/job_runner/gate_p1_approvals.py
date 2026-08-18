"""Approval-ingress probes for the Phase-1 gate (Todo 46).

The automated gate must NEVER mint an operator record: automation-class and
non-TTY ingress refusals are captured as raw evidence, and the fixture seam
record stays fixture-marked and never authorizes the operator gate.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Final

from services.approvals.ingress import (
    IngressRefusalError,
    record_fixture_operation,
    record_operation,
)
from services.approvals.store import OperationRecordStore
from services.approvals.verify import evaluate_authorization
from services.foundation_io import atomic_write, canonical_model_bytes
from services.job_runner.gate_cp_models import GateObservation, OperationOutcome
from services.job_runner.gate_p1_models import APPROVALS_OBSERVATION

ACTOR: Final = "p1-gate-actor"


def _refusal(name: str, action: Callable[[], object]) -> OperationOutcome:
    try:
        action()
        return OperationOutcome(name=name, result="unexpected-success")
    except IngressRefusalError as error:
        return OperationOutcome(name=name, result=error.code, detail=error.detail)


def drive_approvals(evidence: Path) -> Path:
    """Approval-ingress probes: the automated gate never mints operator records."""

    work = evidence / APPROVALS_OBSERVATION
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    target = hashlib.sha256(b"phase-1-gate-approvals-target-v1").hexdigest()
    operations = [
        _refusal(
            "automation-ingress-refused",
            lambda: record_operation(
                purpose="editorial",
                target_bundle_hash=target,
                decision="approve",
                actor_id=ACTOR,
                tty_fd=None,
                runner_class="automation",
            ),
        )
    ]
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        operations.append(
            _refusal(
                "non-tty-ingress-refused",
                lambda: record_operation(
                    purpose="editorial",
                    target_bundle_hash=target,
                    decision="approve",
                    actor_id=ACTOR,
                    tty_fd=devnull,
                ),
            )
        )
    finally:
        os.close(devnull)
    draft = record_fixture_operation(
        purpose="editorial",
        target_bundle_hash=target,
        decision="approve",
        actor_id=ACTOR,
    )
    store = OperationRecordStore(work / "operation-records.jsonl")
    record = store.append(draft)
    fields = {"fixture_record_fixture_only": str(record.fixture_only).lower()}
    verdict = evaluate_authorization(
        store.all_records(),
        purpose="editorial",
        target_hash=target,
        target_type="edit-plan",
        operator_gate=True,
    )
    operations.append(
        OperationOutcome(
            name="operator-gate-fixture-refused",
            result=verdict.refusal_code or "authorized",
            detail=verdict.record_id or "",
        )
    )
    observation = GateObservation(
        fixture_id=APPROVALS_OBSERVATION,
        kind="approval-ingress",
        work_dir=str(work),
        operations=tuple(operations),
        fields=fields,
    )
    target_file = work / "observation.json"
    atomic_write(target_file, canonical_model_bytes(observation))
    return target_file


__all__ = ["drive_approvals"]
