"""Transports for the Editorial Director (Todo 39, Todo-29 pattern).

- ``ReplayTransport``: the PRIMARY and DEFAULT path. Deterministic,
  in-process, zero network. Recorded fixtures map ``request_hash`` (frozen
  prompt contract version + evidence lineage + pin version) to a canned
  strict structured-output payload, a refusal, or a transport failure; an
  unknown hash is an explicit ``replay_mismatch`` failure (stale fixture
  drift), never a silent success.
- ``LiveTransport``: a network-free stub. It performs ZERO network calls
  unless the synthetic-data policy env AND a credential env are BOTH present;
  even then this repository pins no HTTP client for the editorial model, so
  it refuses with an honest ``model_not_executable`` rather than fabricating
  a response. No socket, HTTP, or subprocess import exists here by design.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from services.foundation_io import canonical_model_bytes

if TYPE_CHECKING:
    from services.editorial.pin import EditorialReplaySet

POLICY_ENV = "EDITORIAL_DIRECTOR_CLOUD_FIXTURE"
CREDENTIALS_ENV = "EDITORIAL_DIRECTOR_API_KEY"

_MODEL_NOT_EXECUTABLE = (
    "the pinned editorial transport is a network-free stub in this repository: no HTTP "
    "client is pinned for the editorial model surface, so no live call can be executed; "
    "refusing to fabricate a response — the deterministic replay transport is the only "
    "executable path"
)


@dataclass(frozen=True, slots=True)
class EditorialStrictResponse:
    payload: bytes
    served_by: str


@dataclass(frozen=True, slots=True)
class EditorialModelRefusal:
    reason: str


@dataclass(frozen=True, slots=True)
class EditorialTransportFailure:
    code: str
    detail: str


type EditorialOutcome = EditorialStrictResponse | EditorialModelRefusal | EditorialTransportFailure


class EditorialTransport(Protocol):
    def send(self, request_hash: str) -> EditorialOutcome: ...


@dataclass(slots=True)
class ReplayTransport:
    fixtures: Mapping[str, EditorialOutcome]
    calls: int = field(default=0)

    def send(self, request_hash: str) -> EditorialOutcome:
        self.calls += 1
        outcome = self.fixtures.get(request_hash)
        if outcome is None:
            return EditorialTransportFailure(
                code="replay_mismatch",
                detail=(
                    f"no recorded fixture for request hash {request_hash}; replay fixtures "
                    "are keyed by the exact request hash (stale fixture drift)"
                ),
            )
        return outcome


class LiveTransport:
    """Env-gated live stub; never fabricates and never opens a socket."""

    def send(self, request_hash: str) -> EditorialOutcome:
        if os.environ.get(POLICY_ENV) != "1":
            return EditorialTransportFailure(
                code="live_disabled",
                detail=(
                    f"live editorial transport disabled: synthetic-data policy env "
                    f"{POLICY_ENV}=1 is not set (deny-by-default)"
                ),
            )
        if not os.environ.get(CREDENTIALS_ENV):
            return EditorialTransportFailure(
                code="no_credentials",
                detail=(
                    f"live editorial transport disabled: {CREDENTIALS_ENV} is unset; "
                    "credential VALUES are never read into logs or records"
                ),
            )
        return EditorialTransportFailure(
            code="model_not_executable", detail=f"request {request_hash}: {_MODEL_NOT_EXECUTABLE}"
        )


def replay_response(replay_set: EditorialReplaySet, episode_id: str) -> EditorialStrictResponse:
    """The recorded strict outcome for one episode, served by the replay set."""

    proposal = replay_set.proposal_for(episode_id)
    return EditorialStrictResponse(
        payload=canonical_model_bytes(proposal),
        served_by=f"replay:{replay_set.replay_set_id}",
    )


__all__ = [
    "CREDENTIALS_ENV",
    "POLICY_ENV",
    "EditorialModelRefusal",
    "EditorialOutcome",
    "EditorialStrictResponse",
    "EditorialTransport",
    "EditorialTransportFailure",
    "LiveTransport",
    "ReplayTransport",
    "replay_response",
]
