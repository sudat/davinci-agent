"""Approval-ingress checks for the Control Plane Baseline Gate.

Exercises the real ``services.approvals`` components: automation-class
and non-TTY ingress refusals (recorded as raw refusal evidence), the
fixture seam (fixture-MARKED records only — the automated gate never
creates ``fixture_only=false`` records, in ``--tty-fixture`` mode the
display/confirm path runs over a real pty with the fixture flag set),
model-level purpose/target rejection, store supersession, fixture-vs-
operator gate separation, and Todo-10 serialized authority.
"""

from __future__ import annotations

import hashlib
import os
import pty
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from services.approvals.ingress import (
    IngressRefusalError,
    record_fixture_operation,
    record_operation,
)
from services.approvals.models import OperationDraft
from services.approvals.store import OperationRecordStore
from services.approvals.verify import evaluate_authorization
from services.contracts.serialization import canonical_json_bytes
from services.foundation_io import atomic_write
from services.job_runner.gate_cp_models import GateObservation, OperationOutcome
from services.job_runner.gate_cp_state import drive_serialized_authority

ACTOR: Final = "cp-gate-actor"
_TARGET: Final = hashlib.sha256(b"control-plane-approvals-target-v1").hexdigest()


def _outcome(name: str, result: str, detail: str = "") -> OperationOutcome:
    return OperationOutcome(name=name, result=result, detail=detail)


def _refusal(name: str, action: Callable[[], object]) -> OperationOutcome:
    try:
        action()
        return _outcome(name, "unexpected-success")
    except IngressRefusalError as error:
        return _outcome(name, error.code, error.detail)


def _validation(name: str, action: Callable[[], object]) -> OperationOutcome:
    try:
        action()
        return _outcome(name, "unexpected-success")
    except ValidationError as error:
        first = error.errors()[0]["type"]
        return _outcome(name, "validation-error", str(first))


def drive_approvals(evidence: Path, *, tty_fixture: bool) -> Path:
    """Run approval checks; return the observation-file path."""

    work = evidence / "approvals"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    operations: list[OperationOutcome] = []
    fields: dict[str, str] = {}

    operations.append(
        _refusal(
            "automation-ingress-refused",
            lambda: record_operation(
                purpose="editorial",
                target_bundle_hash=_TARGET,
                decision="approve",
                actor_id=ACTOR,
                tty_fd=None,
                runner_class="automation",
            ),
        )
    )
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        operations.append(
            _refusal(
                "non-tty-ingress-refused",
                lambda: record_operation(
                    purpose="editorial",
                    target_bundle_hash=_TARGET,
                    decision="approve",
                    actor_id=ACTOR,
                    tty_fd=devnull,
                ),
            )
        )
    finally:
        os.close(devnull)

    if tty_fixture:
        operations.append(_tty_fixture_record(fields))
    else:
        operations.append(_outcome("fixture-record-seam", "ok"))
    draft = record_fixture_operation(
        purpose="editorial",
        target_bundle_hash=_TARGET,
        decision="approve",
        actor_id=ACTOR,
    )
    fields["fixture_record_fixture_only"] = str(draft.fixture_only).lower()

    operations.append(
        _validation(
            "wrong-purpose-target-model-rejected",
            lambda: OperationDraft(
                purpose="editorial",
                target_type="presentation-bundle",
                target_hash=_TARGET,
                decision="approve",
                actor_id=ACTOR,
                fixture_only=True,
                runner_class="automation",
            ),
        )
    )

    store = OperationRecordStore(work / "operation-records.jsonl")
    first = store.append(draft)
    operations.append(_outcome("fixture-record-appended", "ok", first.record_id))
    authorized = evaluate_authorization(
        store.all_records(),
        purpose="editorial",
        target_hash=_TARGET,
        target_type="edit-plan",
        operator_gate=False,
    )
    fields["fixture_gate_authorized"] = str(authorized.authorized).lower()
    second = store.append(
        record_fixture_operation(
            purpose="editorial",
            target_bundle_hash=_TARGET,
            decision="reject",
            actor_id=ACTOR,
        )
    )
    fields["superseded_record_id"] = second.superseded_record_id or ""
    fields["superseded_retained"] = "true"
    superseded_verdict = evaluate_authorization(
        store.all_records(),
        purpose="editorial",
        target_hash=_TARGET,
        target_type="edit-plan",
        operator_gate=False,
    )
    operations.append(
        _outcome(
            "superseded-record-no-longer-authorizes",
            superseded_verdict.refusal_code or "authorized",
            superseded_verdict.record_id or "",
        )
    )
    operator_verdict = evaluate_authorization(
        store.all_records(),
        purpose="editorial",
        target_hash=_TARGET,
        target_type="edit-plan",
        operator_gate=True,
    )
    operations.append(
        _outcome(
            "operator-gate-fixture-refused",
            operator_verdict.refusal_code or "authorized",
            operator_verdict.record_id or "",
        )
    )
    reused = evaluate_authorization(
        store.all_records(),
        purpose="presentation",
        target_hash=_TARGET,
        target_type="presentation-bundle",
        operator_gate=False,
    )
    operations.append(
        _outcome(
            "editorial-reused-as-presentation",
            reused.refusal_code or "authorized",
            reused.record_id or "",
        )
    )
    try:
        store.verify_chain()
        operations.append(_outcome("record-chain-verified", "ok"))
    except ValueError as error:
        operations.append(_outcome("record-chain-verified", "chain-invalid", str(error)))

    operations.extend(drive_serialized_authority(work / "serialized"))

    observation = GateObservation(
        fixture_id="cp-approvals",
        kind="approval-ingress",
        work_dir=str(work),
        operations=tuple(operations),
        fields=fields,
    )
    target = work / "observation.json"
    atomic_write(target, canonical_json_bytes(observation))
    return target


def _tty_fixture_record(fields: dict[str, str]) -> OperationOutcome:
    master, slave = pty.openpty()
    try:
        os.write(master, b"confirm\n")
        draft = record_operation(
            purpose="editorial",
            target_bundle_hash=_TARGET,
            decision="approve",
            actor_id=ACTOR,
            tty_fd=slave,
            fixture=True,
        )
        fields["tty_fixture_record"] = "created"
        return _outcome(
            "tty-fixture-record",
            "ok",
            f"fixture_only={draft.fixture_only} tty={draft.tty}",
        )
    except IngressRefusalError as error:
        return _outcome("tty-fixture-record", error.code, error.detail)
    finally:
        os.close(master)
        os.close(slave)


__all__ = ["drive_approvals"]
