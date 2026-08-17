"""Strict models and the hash-chained journal for the Stage Runner (Todo 11).

The idempotency key derives deterministically from ``stage_name +
sorted input hashes + runner_version + code_snapshot_id`` (sha256 over
canonical JSON parts): the same committed inputs can never execute
twice, and a changed runner identity can never falsely reuse a
previous output. All timing is logical; journal events chain by sha256
with the event hash zeroed, mirroring the evidence-ledger pattern.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Producer, Sha256, StrictModel
from services.foundation_io import canonical_model_bytes

GENESIS_HASH = "0" * 64

FailureClass = Literal["transient", "permanent", "blocking_human"]
StageRunOutcome = Literal["succeeded", "reused", "recovered", "failed_blocked"]
JournalEventKind = Literal[
    "attempt-failed", "attempt-succeeded", "run-reused", "run-recovered", "run-blocked",
]


class StageRunKey(StrictModel):
    """Stage identity: what ran, on which inputs, by which runner build."""

    stage_name: Identifier
    input_hashes: tuple[Sha256, ...]
    runner_version: Identifier
    code_snapshot_id: Identifier

    @model_validator(mode="before")
    @classmethod
    def sort_input_hashes(cls, value: object) -> object:
        """Canonicalize input order so key equality is order-insensitive."""

        if isinstance(value, dict):
            hashes = value.get("input_hashes")
            if isinstance(hashes, Sequence) and not isinstance(hashes, str | bytes):
                value = {**value, "input_hashes": tuple(sorted(hashes))}
        return value

    @property
    def idempotency_key(self) -> Identifier:
        """sha256 over canonical key parts (the Todo-9 key contract)."""

        parts = {
            "code_snapshot_id": self.code_snapshot_id,
            "input_hashes": list(self.input_hashes),
            "runner_version": self.runner_version,
            "stage_name": self.stage_name,
        }
        canonical = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()


class RetryPolicy(StrictModel):
    """Bounded retry policy; delays are LOGICAL seconds, never sleeps."""

    max_attempts: int = Field(ge=1, strict=True)
    backoff_schedule: tuple[int, ...] = ()

    @model_validator(mode="after")
    def require_non_negative_delays(self) -> RetryPolicy:
        if any(delay < 0 for delay in self.backoff_schedule):
            raise PydanticCustomError("backoff_negative", "backoff delays must be non-negative")
        return self

    def retry_delay(self, failed_attempt: int) -> int:
        """Logical delay after the Nth failed attempt."""
        if not self.backoff_schedule:
            return 0
        return self.backoff_schedule[min(failed_attempt - 1, len(self.backoff_schedule) - 1)]


class LogicalClock(Protocol):
    """Caller-supplied logical clock; the runner never reads wall time."""

    @property
    def now(self) -> int: ...

    def advance(self, seconds: int) -> None: ...


class SequenceClock:
    """Concrete logical clock: a monotonic counter with no wall reading."""

    def __init__(self, start: int = 0) -> None:
        self._now = start

    @property
    def now(self) -> int:
        return self._now

    def advance(self, seconds: int) -> None:
        if seconds < 0:
            raise ValueError("logical clock cannot move backwards")
        self._now += seconds


class RunnerIdentity(StrictModel):
    """Source/runner identity carried by every record and artifact."""

    runner_version: Identifier
    code_snapshot_id: Identifier
    producer: Producer


class StageFnError(Exception):
    """Deterministic stage failure raised from a runner_fn seam."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class StageRunnerError(Exception):
    """Typed Stage Runner refusal (integrity/contract violations)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class StageRunRecord(StrictModel):
    """Structured run record for one completed stage-run decision."""

    key: StageRunKey
    attempt: int = Field(ge=0, strict=True)
    failure_class: FailureClass | None = None
    error_code: Identifier | None = None
    output_artifact_hash: Sha256 | None = None
    started_seq: int = Field(ge=1, strict=True)
    ended_seq: int = Field(ge=1, strict=True)
    identity: RunnerIdentity


class StageRunResult(StrictModel):
    """Outcome of one ``StageRunner.run`` call."""

    outcome: StageRunOutcome
    output_artifact_hash: Sha256 | None = None
    attempts: int = Field(default=0, ge=0, strict=True)
    failure_class: FailureClass | None = None
    error_code: Identifier | None = None
    needs_human: bool = False
    record: StageRunRecord


class StageJournalEvent(StrictModel):
    """One journaled stage-run event (hash-chained, genesis-zeroed hash)."""

    sequence: int = Field(ge=1, strict=True)
    previous_event_hash: Sha256
    event_hash: Sha256
    job_id: Identifier
    stage_name: Identifier
    idempotency_key: Identifier
    kind: JournalEventKind
    attempt: int = Field(ge=0, strict=True)
    failure_class: FailureClass | None = None
    error_code: Identifier | None = None
    output_artifact_hash: Sha256 | None = None
    identity: RunnerIdentity


def hash_event(event: StageJournalEvent) -> str:
    """sha256 over canonical bytes with ``event_hash`` zeroed."""

    payload = event.model_copy(update={"event_hash": GENESIS_HASH})
    return hashlib.sha256(canonical_model_bytes(payload)).hexdigest()


class StageRunJournal:
    """Hash-chained JSONL journal of stage-run events, one file per job."""

    def __init__(self, root: Path, job_id: Identifier) -> None:
        self._path = root / f"{job_id}-stage-runs.jsonl"

    def events(self) -> tuple[StageJournalEvent, ...]:
        """Read and chain-verify the whole journal; fail closed on drift."""

        if not self._path.exists():
            return ()
        events: list[StageJournalEvent] = []
        previous = GENESIS_HASH
        for line in self._path.read_bytes().splitlines():
            try:
                event = StageJournalEvent.model_validate_json(line)
            except ValidationError as error:
                raise StageRunnerError("journal-corrupt", str(error)) from error
            if event.previous_event_hash != previous or event.event_hash != hash_event(event):
                raise StageRunnerError(
                    "journal-chain", f"event {event.sequence} breaks the hash chain"
                )
            events.append(event)
            previous = event.event_hash
        return tuple(events)

    def mint(  # noqa: PLR0913 (journal event fields are the record contract)
        self,
        *,
        job_id: Identifier,
        key: StageRunKey,
        identity: RunnerIdentity,
        kind: JournalEventKind,
        attempt: int,
        failure_class: FailureClass | None = None,
        error_code: Identifier | None = None,
        output_artifact_hash: Sha256 | None = None,
    ) -> StageJournalEvent:
        events = self.events()
        base = StageJournalEvent(
            sequence=(events[-1].sequence + 1) if events else 1,
            previous_event_hash=events[-1].event_hash if events else GENESIS_HASH,
            event_hash=GENESIS_HASH, job_id=job_id, stage_name=key.stage_name,
            idempotency_key=key.idempotency_key, kind=kind, attempt=attempt,
            failure_class=failure_class, error_code=error_code,
            output_artifact_hash=output_artifact_hash, identity=identity,
        )
        return base.model_copy(update={"event_hash": hash_event(base)})

    def append(self, event: StageJournalEvent) -> None:
        self.events()  # full-chain re-verification before append
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("ab") as stream:
            stream.write(canonical_model_bytes(event) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())


__all__ = [
    "GENESIS_HASH",
    "FailureClass",
    "JournalEventKind",
    "LogicalClock",
    "RetryPolicy",
    "RunnerIdentity",
    "SequenceClock",
    "StageFnError",
    "StageJournalEvent",
    "StageRunJournal",
    "StageRunKey",
    "StageRunOutcome",
    "StageRunRecord",
    "StageRunResult",
    "StageRunnerError",
    "hash_event",
]
