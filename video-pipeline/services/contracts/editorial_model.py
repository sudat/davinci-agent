"""Contract-only pin for the Phase-1 editorial model (role: Editorial Director).

No credentials exist in this repository and no live call is attempted here:
Todo 39 wires the real transport. This module freezes the CONTRACT surface —
the structured-output schema and the request envelope family (mirroring the
Todo-29 ``translator-record-0c-v1`` evidence record family) — plus a canned
replay fixture the toolchain smoke round-trips deterministically offline.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from services.contracts.primitives import Identifier, StrictModel
from services.foundation_io import canonical_model_bytes

STRUCTURED_OUTPUT_SCHEMA_ID = "editorial-selection-proposal"
STRUCTURED_OUTPUT_SCHEMA_VERSION = "v1"
REQUEST_ENVELOPE_SCHEMA_ID = "editorial-request-envelope"
REQUEST_ENVELOPE_SCHEMA_VERSION = "v1"
REQUEST_ENVELOPE_FAMILY = "translator-record-0c-v1"
EDITORIAL_MODEL_REPLAY_ID = "editorial-model-replay-v1"

type SelectionAction = Literal["selected", "dropped"]


class SelectionEntry(StrictModel):
    segment_id: Identifier
    action: SelectionAction
    reason_code: Identifier


class EditorialSelectionProposal(StrictModel):
    """Structured output the editorial model must return (schema v1)."""

    schema_version: Literal["editorial-selection-proposal-v1"]
    proposal_id: Identifier
    episode_id: Identifier
    actor_intent: Literal["model"]
    selection: tuple[SelectionEntry, ...] = Field(min_length=1)
    confidence: tuple[int, int]


class EditorialRequestEnvelope(StrictModel):
    """Request/evidence envelope mirroring the Todo-29 translator record family."""

    schema_version: Literal["editorial-request-envelope-v1"] = "editorial-request-envelope-v1"
    episode_id: Identifier
    model_role_id: Identifier
    prompt_contract_version: str
    request_hash: str
    response_hash: str | None
    status: Literal["proposal", "refusal", "error"]
    external_credentials: Literal["none"]


class EditorialReplayFixture(StrictModel):
    replay_id: Literal["editorial-model-replay-v1"] = "editorial-model-replay-v1"
    envelope: EditorialRequestEnvelope
    proposal: EditorialSelectionProposal


def resolve_structured_output_schema(schema_id: str) -> type[EditorialSelectionProposal]:
    if schema_id != STRUCTURED_OUTPUT_SCHEMA_ID:
        raise LookupError(f"unknown structured-output schema id: {schema_id}")
    return EditorialSelectionProposal


def resolve_request_envelope_schema(schema_id: str) -> type[EditorialRequestEnvelope]:
    if schema_id != REQUEST_ENVELOPE_SCHEMA_ID:
        raise LookupError(f"unknown request-envelope schema id: {schema_id}")
    return EditorialRequestEnvelope


CANNED_REPLAY = EditorialReplayFixture(
    envelope=EditorialRequestEnvelope(
        episode_id="p1-ref-01-clean-ja",
        model_role_id="editorial-director",
        prompt_contract_version="phase-1-editorial-v1",
        request_hash="0" * 64,
        response_hash=None,
        status="proposal",
        external_credentials="none",
    ),
    proposal=EditorialSelectionProposal(
        schema_version="editorial-selection-proposal-v1",
        proposal_id="sel-p1-ref-01-0001",
        episode_id="p1-ref-01-clean-ja",
        actor_intent="model",
        selection=(
            SelectionEntry(
                segment_id="s1", action="selected", reason_code="score-above-threshold"
            ),
            SelectionEntry(segment_id="s2", action="dropped", reason_code="filler"),
        ),
        confidence=(9, 10),
    ),
)


def round_trip_replay() -> EditorialReplayFixture:
    """Re-parse the canned replay from its canonical bytes and byte-bind it."""

    raw = canonical_model_bytes(CANNED_REPLAY)
    replay = EditorialReplayFixture.model_validate_json(raw)
    if canonical_model_bytes(replay) != raw:
        raise ValueError("editorial-model replay round-trip is not byte-stable")
    resolve_structured_output_schema(STRUCTURED_OUTPUT_SCHEMA_ID)
    resolve_request_envelope_schema(REQUEST_ENVELOPE_SCHEMA_ID)
    return replay
