"""Append-only Review Event contract for the Phase-0C Spike.

An event log is a JSONL stream sealed by a head-hash file (the evidence
ledger pattern). Event identity is deterministic: ``event_id`` is the sha256
over the canonical bytes of the event content with the id itself zeroed, so
the same decision content always yields the same event id.
"""

from __future__ import annotations

import hashlib
from typing import Final, Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.foundation_io import canonical_model_bytes
from services.review_command.models import ReviewCommandProposal0C, parse_proposal

GENESIS_EVENT_HASH = "0" * 64
type EventKind0C = Literal[
    "proposal_recorded",
    "decision_applied",
    "command_deferred",
    "moment-selection-v2-committed",
]
type EventActor0C = Literal["operator", "model"]
MOMENT_SELECTION_V2_COMMITTED: Final = "moment-selection-v2-committed"


class ReviewEvent0C(StrictModel):
    event_id: Sha256
    sequence: int = Field(ge=1, strict=True)
    kind: EventKind0C
    proposal_json: str = Field(min_length=1, strict=True)
    proposal_sha256: Sha256
    base_plan_version: str = Field(pattern=r"^v[1-9][0-9]*$", strict=True)
    result_plan_version: str | None = Field(default=None, pattern=r"^v[1-9][0-9]*$", strict=True)
    applied: bool
    actor_intent: EventActor0C
    decision_id: Identifier | None = None
    reason: str | None = Field(default=None, min_length=1, strict=True)
    previous_event_hash: Sha256

    @model_validator(mode="after")
    def require_kind_semantics(self) -> ReviewEvent0C:
        if self.kind == "proposal_recorded":
            if self.applied or self.result_plan_version is not None or self.decision_id:
                raise PydanticCustomError(
                    "event_kind",
                    "proposal_recorded events carry no decision outcome",
                )
        elif self.kind == "decision_applied":
            if not self.applied or self.result_plan_version is None:
                raise PydanticCustomError(
                    "event_kind",
                    "decision_applied events must be applied with a result version",
                )
            if self.result_plan_version == self.base_plan_version:
                raise PydanticCustomError(
                    "event_kind",
                    "decision_applied must advance the plan version",
                )
            if self.decision_id is None:
                raise PydanticCustomError(
                    "event_kind",
                    "decision_applied events must record the deciding operator",
                )
        elif self.kind == MOMENT_SELECTION_V2_COMMITTED:
            self._require_moment_selection_semantics()
        else:
            if self.applied or self.result_plan_version is not None:
                raise PydanticCustomError(
                    "event_kind",
                    "command_deferred events never mutate the plan",
                )
            if self.reason is None:
                raise PydanticCustomError(
                    "event_kind",
                    "command_deferred events must state a reason",
                )
        return self

    def _require_moment_selection_semantics(self) -> None:
        """v2 commits are applied, advance the version; decision/reason optional."""

        if not self.applied or self.result_plan_version is None:
            raise PydanticCustomError(
                "event_kind",
                "moment-selection-v2-committed events must be applied with a result version",
            )
        if self.result_plan_version == self.base_plan_version:
            raise PydanticCustomError(
                "event_kind",
                "moment-selection-v2-committed must advance the plan version",
            )


class EventSeal(StrictModel):
    sequence: int = Field(ge=0, strict=True)
    event_id: Sha256


class EventStreamError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def verify_proposal_hash(event: ReviewEvent0C) -> None:
    """Refuse events whose bound payload hash drifted (kind-agnostic check)."""

    if hashlib.sha256(event.proposal_json.encode()).hexdigest() != event.proposal_sha256:
        raise EventStreamError(f"proposal payload hash mismatch at sequence {event.sequence}")


def event_proposal(event: ReviewEvent0C) -> ReviewCommandProposal0C:
    """Parse the bound proposal, refusing streams whose payload hash drifts.

    v2 moment-selection events carry a different proposal contract; their
    payload is opaque here and parsed by ``services.editorial_v2.proposal_validate``.
    """

    verify_proposal_hash(event)
    if event.kind == MOMENT_SELECTION_V2_COMMITTED:
        raise EventStreamError(
            f"event at sequence {event.sequence} carries a v2 moment-selection "
            "proposal payload, not a Phase-0C review command proposal"
        )
    return parse_proposal(event.proposal_json)


def compute_event_id(event: ReviewEvent0C) -> str:
    zeroed = event.model_copy(update={"event_id": GENESIS_EVENT_HASH})
    return hashlib.sha256(canonical_model_bytes(zeroed)).hexdigest()


def build_event(  # noqa: PLR0913 (explicit append-only event fields; kwargs are the contract)
    *,
    sequence: int,
    kind: EventKind0C,
    proposal: ReviewCommandProposal0C,
    base_plan_version: str,
    previous_event_hash: str,
    actor_intent: EventActor0C,
    applied: bool = False,
    result_plan_version: str | None = None,
    decision_id: str | None = None,
    reason: str | None = None,
) -> ReviewEvent0C:
    proposal_json = canonical_model_bytes(proposal).decode()
    draft = ReviewEvent0C(
        event_id=GENESIS_EVENT_HASH,
        sequence=sequence,
        kind=kind,
        proposal_json=proposal_json,
        proposal_sha256=hashlib.sha256(proposal_json.encode()).hexdigest(),
        base_plan_version=base_plan_version,
        result_plan_version=result_plan_version,
        applied=applied,
        actor_intent=actor_intent,
        decision_id=decision_id,
        reason=reason,
        previous_event_hash=previous_event_hash,
    )
    return draft.model_copy(update={"event_id": compute_event_id(draft)})


def parse_event_stream(data: bytes) -> tuple[ReviewEvent0C, ...]:
    """Validate a full JSONL event stream: ids, hash chain, and sequence order."""

    events: list[ReviewEvent0C] = []
    previous_hash = GENESIS_EVENT_HASH
    for expected_sequence, line in enumerate(data.splitlines(), start=1):
        try:
            event = ReviewEvent0C.model_validate_json(line)
            if event.kind == MOMENT_SELECTION_V2_COMMITTED:
                verify_proposal_hash(event)
            else:
                event_proposal(event)
        except EventStreamError:
            raise
        except ValidationError as error:
            raise EventStreamError(f"invalid event line {expected_sequence}: {error}") from error
        if event.sequence != expected_sequence:
            raise EventStreamError(
                f"sequence gap: expected {expected_sequence}, got {event.sequence}"
            )
        if event.previous_event_hash != previous_hash:
            raise EventStreamError(f"broken hash chain at sequence {event.sequence}")
        if event.event_id != compute_event_id(event):
            raise EventStreamError(
                f"event id does not match canonical content at sequence {event.sequence}"
            )
        previous_hash = event.event_id
        events.append(event)
    return tuple(events)


def event_stream_bytes(events: tuple[ReviewEvent0C, ...]) -> bytes:
    return b"".join(canonical_model_bytes(event) + b"\n" for event in events)


__all__ = [
    "GENESIS_EVENT_HASH",
    "MOMENT_SELECTION_V2_COMMITTED",
    "EventSeal",
    "EventStreamError",
    "ReviewEvent0C",
    "build_event",
    "compute_event_id",
    "event_proposal",
    "event_stream_bytes",
    "parse_event_stream",
    "verify_proposal_hash",
]
