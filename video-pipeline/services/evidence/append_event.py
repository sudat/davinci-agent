from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from services.fixtures.models import FreezeEventRow, FreezeIntent
from services.foundation_io import canonical_model_bytes
from services.toolchain.execution_ledger import ExecutionLedgerError, LedgerRow


@dataclass(frozen=True, slots=True)
class LedgerState:
    lines: tuple[bytes, ...]
    rows: tuple[LedgerRow, ...]


def _read_and_repair(descriptor: int) -> LedgerState:
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = os.read(descriptor, os.fstat(descriptor).st_size)
    chunks = raw.splitlines(keepends=True)
    lines: list[bytes] = []
    rows: list[LedgerRow] = []
    consumed = 0
    for index, chunk in enumerate(chunks):
        line = chunk.rstrip(b"\r\n")
        if not line:
            consumed += len(chunk)
            continue
        try:
            row = LedgerRow.model_validate_json(line)
        except ValidationError as error:
            incomplete_tail = index == len(chunks) - 1 and not chunk.endswith((b"\n", b"\r"))
            if incomplete_tail:
                os.ftruncate(descriptor, consumed)
                os.fsync(descriptor)
                break
            detail = f"invalid execution ledger row {index + 1}: {error}"
            raise ExecutionLedgerError(detail) from error
        if line != canonical_model_bytes(row):
            raise ExecutionLedgerError(f"noncanonical execution ledger row {index + 1}")
        expected_sequence = len(rows) + 1
        expected_previous = "0" * 64 if not lines else hashlib.sha256(lines[-1]).hexdigest()
        if row.sequence != expected_sequence or row.previous_event_hash != expected_previous:
            raise ExecutionLedgerError(f"execution ledger chain drift at row {index + 1}")
        lines.append(line)
        rows.append(row)
        consumed += len(chunk)
    if not rows:
        raise ExecutionLedgerError("execution ledger is empty")
    return LedgerState(lines=tuple(lines), rows=tuple(rows))


def _existing_freeze_event(
    state: LedgerState,
    event_type: Literal["freeze-intent", "freeze-completed"],
) -> FreezeEventRow | None:
    for line in state.lines:
        try:
            event = FreezeEventRow.model_validate_json(line)
        except ValidationError:
            continue
        if event.event_type == event_type:
            return event
    return None


def _append_freeze_event(
    descriptor: int,
    state: LedgerState,
    intent: FreezeIntent,
    event_type: Literal["freeze-intent", "freeze-completed"],
) -> FreezeEventRow:
    intent_sha256 = hashlib.sha256(canonical_model_bytes(intent)).hexdigest()
    existing = _existing_freeze_event(state, event_type)
    if existing is not None:
        if (
            existing.intent_sha256 == intent_sha256
            and existing.policy_sha256 == intent.policy_sha256
            and existing.receipt_sha256 == intent.receipt_sha256
        ):
            return existing
        raise ExecutionLedgerError(f"differing duplicate {event_type} for Todo 6 phase-0a v1")
    head_line = state.lines[-1]
    head = state.rows[-1]
    event = FreezeEventRow(
        sequence=head.sequence + 1,
        previous_event_hash=hashlib.sha256(head_line).hexdigest(),
        event_type=event_type,
        intent_sha256=intent_sha256,
        policy_sha256=intent.policy_sha256,
        receipt_sha256=intent.receipt_sha256,
    )
    os.lseek(descriptor, 0, os.SEEK_END)
    os.write(descriptor, canonical_model_bytes(event) + b"\n")
    os.fsync(descriptor)
    return event


def append_freeze_event(
    ledger: Path,
    intent: FreezeIntent,
    event_type: Literal["freeze-intent", "freeze-completed"],
) -> FreezeEventRow:
    descriptor = os.open(ledger, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        state = _read_and_repair(descriptor)
        return _append_freeze_event(descriptor, state, intent, event_type)
    finally:
        os.close(descriptor)


def require_intent_recorded(ledger: Path, intent: FreezeIntent) -> None:
    descriptor = os.open(ledger, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        state = _read_and_repair(descriptor)
        existing = _existing_freeze_event(state, "freeze-intent")
        expected_hash = hashlib.sha256(canonical_model_bytes(intent)).hexdigest()
        if existing is None or existing.intent_sha256 != expected_hash:
            raise ExecutionLedgerError("matching freeze intent is not recorded")
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--event-file", type=Path, required=True)
    parser.add_argument("--fsync", action="store_true", required=True)
    parser.add_argument("--expect-next-sequence", action="store_true", required=True)
    return parser


def _load_intent(path: Path) -> FreezeIntent:
    raw = path.read_bytes()
    intent = FreezeIntent.model_validate_json(raw)
    if raw != canonical_model_bytes(intent):
        raise ExecutionLedgerError("noncanonical freeze intent")
    return intent


def main() -> int:
    arguments = _parser().parse_args()
    try:
        intent = _load_intent(arguments.event_file)
        event = append_freeze_event(arguments.ledger, intent, "freeze-intent")
    except (ExecutionLedgerError, OSError, ValidationError) as error:
        print(error)
        return 2
    print(f"freeze-intent sequence={event.sequence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
