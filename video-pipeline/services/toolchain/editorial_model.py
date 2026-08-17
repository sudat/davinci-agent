"""Contract-only editorial-model toolchain section for Phase-1 Technical.

There are no credentials in this repository, so this pin freezes the CONTRACT
metadata (model role id, API surface, structured-output schema id/version,
request-envelope schema family) with ``external_credentials=none`` and live
verification explicitly deferred to Todo 39. The smoke is fully deterministic
and offline: the contract parses, the schema ids resolve against
``services.contracts``, and the canned replay fixture round-trips byte-stably.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from services.contracts.editorial_model import round_trip_replay
from services.contracts.primitives import StrictModel
from services.foundation_io import atomic_write


class EditorialModelSection(StrictModel):
    schema_version: Literal["editorial-model-v1"]
    model_role_id: Literal["editorial-director"]
    api_surface: Literal["openai-responses-structured-output"]
    structured_output_schema_id: Literal["editorial-selection-proposal"]
    structured_output_schema_version: Literal["v1"]
    request_envelope_schema_id: Literal["editorial-request-envelope"]
    request_envelope_schema_version: Literal["v1"]
    request_envelope_family: Literal["translator-record-0c-v1"]
    replay_fixture_id: Literal["editorial-model-replay-v1"]
    external_credentials: Literal["none"]
    live_verification: Literal["deferred-to-todo-39"]


class EditorialModelSmokeError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def run_editorial_model_smoke(section: EditorialModelSection, smoke_dir: Path) -> Path:
    """Deterministic offline validation; no network, no fabricated live result."""

    smoke_dir.mkdir(parents=True, exist_ok=True)
    evidence = smoke_dir / "editorial-model-smoke.json"
    replay = round_trip_replay()
    payload = {
        "contract": {
            "model_role_id": section.model_role_id,
            "api_surface": section.api_surface,
            "structured_output_schema_id": section.structured_output_schema_id,
            "structured_output_schema_version": section.structured_output_schema_version,
            "request_envelope_schema_id": section.request_envelope_schema_id,
            "request_envelope_schema_version": section.request_envelope_schema_version,
            "request_envelope_family": section.request_envelope_family,
            "external_credentials": section.external_credentials,
            "live_verification": section.live_verification,
        },
        "network_used": False,
        "replay_round_trip": {
            "replay_id": replay.replay_id,
            "envelope_status": replay.envelope.status,
            "proposal_id": replay.proposal.proposal_id,
            "selection_entries": len(replay.proposal.selection),
        },
    }
    atomic_write(
        evidence,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(),
    )
    return evidence


def verify_editorial_model_contract(section: EditorialModelSection) -> None:
    if section.external_credentials != "none":
        raise EditorialModelSmokeError("editorial-model pin must carry no credentials")
    if section.live_verification != "deferred-to-todo-39":
        raise EditorialModelSmokeError("editorial-model live verification must stay deferred")
    round_trip_replay()
