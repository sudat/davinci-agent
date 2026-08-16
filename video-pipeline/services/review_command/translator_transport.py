"""Transports for the phase-0C Review Translator.

- ``ReplayTransport``: the PRIMARY test path. Deterministic, in-process, no
  network. Recorded fixtures map ``request_hash`` to a canned strict response,
  refusal, or transport failure; an unknown hash is a ``replay_mismatch``
  failure (stale-state drift detection) and never a silent success.
- ``LiveTransport``: a network-free stub for the ``cloud_fixture`` lane. It
  performs NO network call unless the explicit synthetic-data policy env AND a
  credentials env are both present; even then the frozen phase-0C pin declares
  ``external_model: none``, so it refuses rather than calling an unpinned
  provider (the concrete production model pin arrives with Todo 32). Every
  branch is bounded constant work — no I/O waits are possible.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

POLICY_ENV = "PHASE0C_TRANSLATOR_CLOUD_FIXTURE"
CREDENTIALS_ENV = "PHASE0C_TRANSLATOR_CREDENTIALS_FILE"


@dataclass(frozen=True, slots=True)
class StrictResponse:
    payload: bytes


@dataclass(frozen=True, slots=True)
class ModelRefusal:
    reason: str


@dataclass(frozen=True, slots=True)
class TransportFailure:
    code: str
    detail: str


type TransportOutcome = StrictResponse | ModelRefusal | TransportFailure


class Transport(Protocol):
    def send(self, request_hash: str) -> TransportOutcome: ...


@dataclass(slots=True)
class ReplayTransport:
    fixtures: Mapping[str, TransportOutcome]
    calls: int = field(default=0)

    def send(self, request_hash: str) -> TransportOutcome:
        self.calls += 1
        outcome = self.fixtures.get(request_hash)
        if outcome is None:
            return TransportFailure(
                code="replay_mismatch",
                detail=(
                    f"no recorded fixture for request hash {request_hash}; replay fixtures "
                    "are keyed by the exact request hash (stale fixture drift)"
                ),
            )
        return outcome


class LiveTransport:
    """Env-gated live stub; makes no network call under the frozen 0C pin."""

    def send(self, request_hash: str) -> TransportOutcome:
        if os.environ.get(POLICY_ENV) != "1":
            return TransportFailure(
                code="live_disabled",
                detail=(
                    f"live translator transport disabled: synthetic-data policy env "
                    f"{POLICY_ENV}=1 is not set (deny-by-default)"
                ),
            )
        location = os.environ.get(CREDENTIALS_ENV)
        if not location or not Path(location).is_file():
            return TransportFailure(
                code="no_credentials",
                detail=(
                    f"live translator transport disabled: {CREDENTIALS_ENV} does not point "
                    "to a credentials file (values are never read into logs)"
                ),
            )
        return TransportFailure(
            code="model_not_pinned",
            detail=(
                f"request {request_hash}: the frozen phase-0C toolchain pin declares "
                "external_model=none; a live call requires the concrete production model "
                "pin from the Phase-1 editorial-model freeze (Todo 32)"
            ),
        )
