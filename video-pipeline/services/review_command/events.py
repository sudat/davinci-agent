"""Append-only Review Event contract for the Phase-0C Spike.

An event log is a JSONL stream sealed by a head-hash file (the evidence
ledger pattern). Event identity is deterministic: ``event_id`` is the sha256
over the canonical bytes of the event content with the id itself zeroed, so
the same decision content always yields the same event id.
"""

# allow: SIZE_OK — the kind semantics and their payload parsers
# (event_proposal / event_restored_plan / event_policy_plan, plus the
# _tuplize coercion the versioned store shares) must stay co-located because
# parse_event_stream routes each sealed line by kind; splitting any payload
# parser out would fork the event contract across two modules.

from __future__ import annotations

import hashlib
import json
from typing import Final, Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.edit_plan_0c import EditPlan0C
from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.foundation_io import canonical_model_bytes
from services.review_command.models import ReviewCommandProposal0C, parse_proposal

GENESIS_EVENT_HASH = "0" * 64
type EventKind0C = Literal[
    "proposal_recorded",
    "decision_applied",
    "command_deferred",
    "moment-selection-v2-committed",
    "plan_restored",
    "policy_applied",
]
type EventActor0C = Literal["operator", "model"]
MOMENT_SELECTION_V2_COMMITTED: Final = "moment-selection-v2-committed"
# UX redesign 工程1: undo is a NEW forward version whose content equals the
# parent's. The restored-from version and the restored plan ride the hash-
# bound payload (never new ReviewEvent0C fields: that would change the
# canonical bytes — and hence the event ids — of every existing sealed log).
# UX redesign 工程1: undo is a NEW forward version whose content equals the
# parent's. The restored-from version and the restored plan ride the hash-
# bound payload (never new ReviewEvent0C fields: that would change the
# canonical bytes — and hence the event ids — of every existing sealed log).
RESTORED_EVENT_KIND: Final = "plan_restored"
# Consultation slice 2: a director re-run under an adopted consultation
# policy commits a WHOLESALE new plan version (the re-derived plan cannot
# be expressed as remove/adjust deltas). Same self-contained payload shape
# as plan_restored: the judgment linkage plus the full derived plan, so the
# reducer folds it without disk reads. Additive: existing kinds untouched.
POLICY_EVENT_KIND: Final = "policy_applied"


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
            self._require_decision_applied_semantics()
        elif self.kind == MOMENT_SELECTION_V2_COMMITTED:
            self._require_moment_selection_semantics()
        elif self.kind == RESTORED_EVENT_KIND:
            self._require_plan_restored_semantics()
        elif self.kind == POLICY_EVENT_KIND:
            self._require_policy_applied_semantics()
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

    def _require_decision_applied_semantics(self) -> None:
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

    def _require_plan_restored_semantics(self) -> None:
        """Restores are applied operator decisions that advance the version."""

        if not self.applied or self.result_plan_version is None:
            raise PydanticCustomError(
                "event_kind",
                "plan_restored events must be applied with a result version",
            )
        if self.result_plan_version == self.base_plan_version:
            raise PydanticCustomError(
                "event_kind",
                "plan_restored must advance the plan version",
            )
        if self.decision_id is None:
            raise PydanticCustomError(
                "event_kind",
                "plan_restored events must record the deciding operator",
            )

    def _require_policy_applied_semantics(self) -> None:
        """Policy commits are applied operator decisions advancing the version."""

        if not self.applied or self.result_plan_version is None:
            raise PydanticCustomError(
                "event_kind",
                "policy_applied events must be applied with a result version",
            )
        if self.result_plan_version == self.base_plan_version:
            raise PydanticCustomError(
                "event_kind",
                "policy_applied must advance the plan version",
            )
        if self.decision_id is None:
            raise PydanticCustomError(
                "event_kind",
                "policy_applied events must record the deciding operator",
            )

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


def _tuplize(value: object) -> object:
    """Coerce parsed JSON arrays to tuples for strict contract models.

    Envelopes with ``mode="before"`` validators parse JSON payloads through
    Python-mode strict validation, where bare ``tuple[...]`` fields reject
    lists; canonical bytes round-trip exactly after this coercion. Lives on
    the events layer because ``store`` imports ``events``, never the reverse.
    """

    if isinstance(value, list):
        return tuple(_tuplize(item) for item in value)
    if isinstance(value, dict):
        return {key: _tuplize(item) for key, item in value.items()}
    return value


def verify_proposal_hash(event: ReviewEvent0C) -> None:
    """Refuse events whose bound payload hash drifted (kind-agnostic check)."""

    if hashlib.sha256(event.proposal_json.encode()).hexdigest() != event.proposal_sha256:
        raise EventStreamError(f"proposal payload hash mismatch at sequence {event.sequence}")


def event_proposal(event: ReviewEvent0C) -> ReviewCommandProposal0C:
    """Parse the bound proposal, refusing streams whose payload hash drifts.

    v2 moment-selection and plan-restored events carry different payload
    contracts; their payloads are opaque here — the v2 payload is parsed by
    ``services.editorial_v2.proposal_validate`` and the restore payload by
    ``event_restored_plan``.
    """

    verify_proposal_hash(event)
    if event.kind == MOMENT_SELECTION_V2_COMMITTED:
        raise EventStreamError(
            f"event at sequence {event.sequence} carries a v2 moment-selection "
            "proposal payload, not a Phase-0C review command proposal"
        )
    if event.kind == RESTORED_EVENT_KIND:
        raise EventStreamError(
            f"event at sequence {event.sequence} carries a plan-restored "
            "payload, not a Phase-0C review command proposal; parse it with "
            "event_restored_plan"
        )
    if event.kind == POLICY_EVENT_KIND:
        raise EventStreamError(
            f"event at sequence {event.sequence} carries a policy-applied "
            "payload, not a Phase-0C review command proposal; parse it with "
            "event_policy_plan"
        )
    return parse_proposal(event.proposal_json)


def restore_payload(restored_from_version: int, plan: EditPlan0C) -> str:
    """Canonical opaque payload for ``plan_restored`` events: the restored-from
    version plus the full restored plan (hash-bound, self-contained so the
    reducer can fold a restore without disk reads)."""

    return json.dumps(
        {
            "restored_from_version": f"v{restored_from_version}",
            "plan": json.loads(canonical_model_bytes(plan)),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def event_restored_plan(event: ReviewEvent0C) -> EditPlan0C:
    """Parse the bound restore payload, refusing streams whose hash drifts."""

    if event.kind != RESTORED_EVENT_KIND:
        raise EventStreamError(
            f"event at sequence {event.sequence} is not a plan restore"
        )
    verify_proposal_hash(event)
    try:
        payload = json.loads(event.proposal_json)
        restored_from = payload["restored_from_version"]
        plan_json = payload["plan"]
    except (ValueError, KeyError, TypeError) as error:
        raise EventStreamError(
            f"malformed restore payload at sequence {event.sequence}"
        ) from error
    if not isinstance(restored_from, str) or not isinstance(plan_json, dict):
        raise EventStreamError(
            f"malformed restore payload at sequence {event.sequence}"
        )
    try:
        return EditPlan0C.model_validate(_tuplize(plan_json))
    except (ValidationError, ValueError) as error:
        raise EventStreamError(
            f"unparsable restored plan at sequence {event.sequence}"
        ) from error


def policy_payload(
    judgment_id: str, proposal_id: str | None, decision: str, plan: EditPlan0C
) -> str:
    """Canonical opaque payload for ``policy_applied`` events: the adopting
    judgment linkage plus the full derived plan (hash-bound, self-contained
    so the reducer folds a policy commit without disk reads)."""

    return json.dumps(
        {
            "judgment_id": judgment_id,
            "proposal_id": proposal_id,
            "decision": decision,
            "plan": json.loads(canonical_model_bytes(plan)),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def event_policy_plan(event: ReviewEvent0C) -> EditPlan0C:
    """Parse the bound policy payload, refusing streams whose hash drifts."""

    if event.kind != POLICY_EVENT_KIND:
        raise EventStreamError(
            f"event at sequence {event.sequence} is not a policy commit"
        )
    verify_proposal_hash(event)
    try:
        payload = json.loads(event.proposal_json)
        plan_json = payload["plan"]
    except (ValueError, KeyError, TypeError) as error:
        raise EventStreamError(
            f"malformed policy payload at sequence {event.sequence}"
        ) from error
    if not isinstance(plan_json, dict):
        raise EventStreamError(
            f"malformed policy payload at sequence {event.sequence}"
        )
    try:
        return EditPlan0C.model_validate(_tuplize(plan_json))
    except (ValidationError, ValueError) as error:
        raise EventStreamError(
            f"unparsable policy plan at sequence {event.sequence}"
        ) from error


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
            elif event.kind == RESTORED_EVENT_KIND:
                event_restored_plan(event)
            elif event.kind == POLICY_EVENT_KIND:
                event_policy_plan(event)
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
    "POLICY_EVENT_KIND",
    "RESTORED_EVENT_KIND",
    "EventSeal",
    "EventStreamError",
    "ReviewEvent0C",
    "build_event",
    "compute_event_id",
    "event_policy_plan",
    "event_proposal",
    "event_restored_plan",
    "event_stream_bytes",
    "parse_event_stream",
    "policy_payload",
    "restore_payload",
    "verify_proposal_hash",
]
