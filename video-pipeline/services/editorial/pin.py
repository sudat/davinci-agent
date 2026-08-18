"""Concrete Editorial Director model pin (Todo 39; extends the Todo-32 freeze).

The Todo-32 contract pin (``config/toolchains/pins/editorial-model.json``,
embedded in the frozen phase-1-technical lock) froze the CONTRACT surface
with ``live_verification=deferred-to-todo-39``. This module adds the CONCRETE
runtime pin as a sibling file — ``config/toolchains/pins/editorial-director.json``
— without touching any frozen lock or pin bytes. The concrete pin is
cross-checked against the frozen lock's ``editorial_model`` section on every
load so the two can never drift apart. No credentials exist in this
repository: ``external_credentials`` lists env-var NAMES ONLY and live
verification stays honestly deferred until real credentials exist.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from services.contracts.editorial_model import (  # noqa: TC001 (pydantic runtime field)
    EditorialSelectionProposal,
)
from services.contracts.primitives import Identifier, Sha256, StrictModel
from services.fixtures.manifest_phase1 import PHASE_1_FIXTURE_IDS
from services.foundation_io import canonical_model_bytes

PIN_PATH = Path("config/toolchains/pins/editorial-director.json")
FROZEN_LOCK_PATH = Path("config/toolchains/phase-1-technical-v1.json")

_ENV_NAME_PATTERN = "must be an env-var NAME (UPPER_SNAKE), never a value"


class PinError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class EditorialDirectorPin(StrictModel):
    """Concrete model pin: role, model id, surface, schemas, fixture hashes."""

    schema_version: Literal["editorial-director-pin-v1"]
    pin_version: Literal["editorial-director-v1"]
    model_role_id: Literal["editorial-director"]
    model_id: str = Field(min_length=1, strict=True)
    api_surface: Literal["openai-responses-structured-output"]
    structured_output_schema_id: Literal["editorial-selection-proposal"]
    structured_output_schema_version: Literal["v1"]
    request_envelope_schema_id: Literal["editorial-request-envelope"]
    request_envelope_schema_version: Literal["v1"]
    replay_set_path: str = Field(min_length=1, strict=True)
    replay_set_sha256: Sha256
    external_credentials: tuple[str, ...] = Field(min_length=1)
    live_verification: Literal["deferred-until-credentials"]

    @field_validator("external_credentials")
    @classmethod
    def names_not_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if not name.isupper() or " " in name or "=" in name:
                raise ValueError(f"external_credentials entry {name!r} {_ENV_NAME_PATTERN}")
        return value


class ReplayDerivation(StrictModel):
    basis: Literal["frozen-golden-expected-table"]
    source_path: str = Field(min_length=1, strict=True)
    source_sha256: Sha256


class ReplayFixture(StrictModel):
    episode_id: Identifier
    proposal: EditorialSelectionProposal


class EditorialReplaySet(StrictModel):
    """Manifest-derived replay responses; NEVER authored from model output."""

    schema_version: Literal["editorial-replay-fixtures-v1"]
    replay_set_id: Identifier
    derived_from: ReplayDerivation
    fixtures: tuple[ReplayFixture, ...] = Field(min_length=1)

    def proposal_for(self, episode_id: str) -> EditorialSelectionProposal:
        for fixture in self.fixtures:
            if fixture.episode_id == episode_id:
                return fixture.proposal
        raise PinError(
            "replay-episode-unknown", f"replay set has no fixture for episode {episode_id}"
        )


def _read_canonical[ModelT: StrictModel](
    path: Path, model: type[ModelT], what: str
) -> tuple[ModelT, bytes]:
    try:
        raw = path.read_bytes()
        parsed = model.model_validate_json(raw)
    except OSError as error:
        raise PinError("pin-unreadable", f"cannot read {what} at {path}: {error}") from error
    except ValueError as error:
        raise PinError("pin-invalid", f"{what} at {path} is malformed: {error}") from error
    if raw != canonical_model_bytes(parsed):
        raise PinError("pin-noncanonical", f"{what} at {path} is not canonical JSON")
    return parsed, raw


def _verify_frozen_contract(pin: EditorialDirectorPin) -> None:
    try:
        document: object = json.loads(FROZEN_LOCK_PATH.read_bytes())
    except OSError as error:
        raise PinError(
            "frozen-lock-unreadable", f"cannot read {FROZEN_LOCK_PATH}: {error}"
        ) from error
    if not isinstance(document, dict) or not isinstance(document.get("editorial_model"), dict):
        raise PinError("frozen-lock-invalid", "lock has no editorial_model section")
    frozen: object = document["editorial_model"]
    if not isinstance(frozen, dict):
        raise PinError("frozen-lock-invalid", "editorial_model section is not an object")
    expected = {
        "model_role_id": pin.model_role_id,
        "api_surface": pin.api_surface,
        "structured_output_schema_id": pin.structured_output_schema_id,
        "structured_output_schema_version": pin.structured_output_schema_version,
        "request_envelope_schema_id": pin.request_envelope_schema_id,
        "request_envelope_schema_version": pin.request_envelope_schema_version,
    }
    for field, value in expected.items():
        if frozen.get(field) != value:
            raise PinError(
                "pin-drift",
                f"concrete pin {field}={value!r} disagrees with the frozen lock value "
                f"{frozen.get(field)!r}",
            )


def load_pin(path: Path = PIN_PATH) -> EditorialDirectorPin:
    pin, _raw = _read_canonical(path, EditorialDirectorPin, "editorial-director pin")
    _verify_frozen_contract(pin)
    return pin


def load_replay_set(pin: EditorialDirectorPin, path: Path | None = None) -> EditorialReplaySet:
    replay_path = path if path is not None else Path(pin.replay_set_path)
    try:
        raw = replay_path.read_bytes()
    except OSError as error:
        raise PinError("replay-unreadable", f"cannot read replay set: {error}") from error
    digest = hashlib.sha256(raw).hexdigest()
    if digest != pin.replay_set_sha256:
        raise PinError(
            "replay-set-hash-drift",
            f"replay set {replay_path} sha256 {digest} does not match the pinned "
            f"{pin.replay_set_sha256}; refusing stale fixtures",
        )
    replay_set, canonical = _read_canonical(replay_path, EditorialReplaySet, "replay set")
    covered = tuple(fixture.episode_id for fixture in replay_set.fixtures)
    if covered != PHASE_1_FIXTURE_IDS:
        raise PinError(
            "replay-set-coverage",
            f"replay set episodes {covered} do not exactly cover the frozen Phase-1 "
            f"fixtures {PHASE_1_FIXTURE_IDS}",
        )
    if digest != hashlib.sha256(canonical).hexdigest():
        raise PinError("replay-set-hash-drift", "replay set bytes changed while loading")
    return replay_set


__all__ = [
    "FROZEN_LOCK_PATH",
    "PIN_PATH",
    "EditorialDirectorPin",
    "EditorialReplaySet",
    "PinError",
    "ReplayDerivation",
    "ReplayFixture",
    "load_pin",
    "load_replay_set",
]
