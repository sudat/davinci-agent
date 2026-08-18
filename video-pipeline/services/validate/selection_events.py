"""Sealed append-only Selection commit event log (Todo-30 family pattern).

Event identity is deterministic: ``event_id`` is the sha256 over the
canonical bytes of the event content with the id itself zeroed, so the
same commit content always yields the same event id (idempotent replay
detection). The log is a JSONL stream sealed by a head-hash file.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.foundation_io import canonical_model_bytes
from services.review_command.events import GENESIS_EVENT_HASH, EventSeal

type SelectionEventKind = Literal["plan_committed", "commit_refused"]


class SelectionCommitEvent(StrictModel):
    event_id: Sha256
    sequence: int = Field(ge=1, strict=True)
    kind: SelectionEventKind
    episode_id: Identifier
    proposal_digest: Sha256
    proposal_base_version: str | None = Field(default=None, min_length=1, strict=True)
    result_version: str | None = Field(
        default=None, pattern=r"^v[1-9][0-9]*$", strict=True
    )
    refusal_validator: str | None = Field(default=None, min_length=1, strict=True)
    refusal_code: str | None = Field(default=None, min_length=1, strict=True)
    previous_event_hash: Sha256

    @model_validator(mode="after")
    def require_kind_semantics(self) -> SelectionCommitEvent:
        if self.kind == "plan_committed":
            if (
                self.result_version is None
                or self.proposal_base_version is None
                or self.refusal_validator
                or self.refusal_code
            ):
                raise PydanticCustomError(
                    "event_kind",
                    "plan_committed events carry a base and result version, no refusal",
                )
        elif (
            self.refusal_validator is None
            or self.refusal_code is None
            or self.result_version is not None
        ):
            raise PydanticCustomError(
                "event_kind",
                "commit_refused events carry a refusal and no result version",
            )
        return self


class SelectionEventStreamError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def compute_selection_event_id(event: SelectionCommitEvent) -> str:
    zeroed = event.model_copy(update={"event_id": GENESIS_EVENT_HASH})
    return hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()


def build_selection_event(  # noqa: PLR0913 (append-only event fields; kwargs are the contract)
    *,
    sequence: int,
    kind: SelectionEventKind,
    episode_id: str,
    proposal_digest: str,
    proposal_base_version: str | None,
    previous_event_hash: str,
    result_version: str | None = None,
    refusal_validator: str | None = None,
    refusal_code: str | None = None,
) -> SelectionCommitEvent:
    draft = SelectionCommitEvent(
        event_id=GENESIS_EVENT_HASH,
        sequence=sequence,
        kind=kind,
        episode_id=episode_id,
        proposal_digest=proposal_digest,
        proposal_base_version=proposal_base_version,
        result_version=result_version,
        refusal_validator=refusal_validator,
        refusal_code=refusal_code,
        previous_event_hash=previous_event_hash,
    )
    return draft.model_copy(update={"event_id": compute_selection_event_id(draft)})


def parse_selection_event_stream(data: bytes) -> tuple[SelectionCommitEvent, ...]:
    """Validate a full JSONL event stream: ids, hash chain, and sequence order."""

    events: list[SelectionCommitEvent] = []
    previous_hash = GENESIS_EVENT_HASH
    for expected_sequence, line in enumerate(data.splitlines(), start=1):
        try:
            event = SelectionCommitEvent.model_validate_json(line)
        except ValidationError as error:
            raise SelectionEventStreamError(
                f"invalid event line {expected_sequence}: {error}"
            ) from error
        if event.sequence != expected_sequence:
            raise SelectionEventStreamError(
                f"sequence gap: expected {expected_sequence}, got {event.sequence}"
            )
        if event.previous_event_hash != previous_hash:
            raise SelectionEventStreamError(f"broken hash chain at sequence {event.sequence}")
        if event.event_id != compute_selection_event_id(event):
            raise SelectionEventStreamError(
                f"event id does not match canonical content at sequence {event.sequence}"
            )
        previous_hash = event.event_id
        events.append(event)
    return tuple(events)


def event_stream_bytes(events: tuple[SelectionCommitEvent, ...]) -> bytes:
    return b"".join(canonical_model_bytes(event) + b"\n" for event in events)


def seal_for(events: tuple[SelectionCommitEvent, ...]) -> EventSeal:
    if not events:
        return EventSeal(sequence=0, event_id=GENESIS_EVENT_HASH)
    return EventSeal(sequence=events[-1].sequence, event_id=events[-1].event_id)


__all__ = [
    "GENESIS_EVENT_HASH",
    "SelectionCommitEvent",
    "SelectionEventKind",
    "SelectionEventStreamError",
    "build_selection_event",
    "compute_selection_event_id",
    "event_stream_bytes",
    "parse_selection_event_stream",
    "seal_for",
]
