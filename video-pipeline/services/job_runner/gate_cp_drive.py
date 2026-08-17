"""Shared driving helpers for Control Plane gate scenario execution."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Final

from services.artifact_store.lease import LeaseError
from services.artifact_store.models import PublicationIntent
from services.artifact_store.store import StoreRefusalError
from services.contracts.primitives import ArtifactEnvelope, Producer
from services.contracts.serialization import canonical_json_bytes
from services.foundation_io import atomic_write
from services.job_runner.gate_cp_models import GateObservation, OperationOutcome
from services.job_runner.state_errors import StateStoreError

GATE_PRODUCER: Final = Producer(name="control-plane-gate", version="v1")


def intent_for(
    artifact_id: str, content_sha256: str
) -> PublicationIntent:
    return PublicationIntent(
        envelope=ArtifactEnvelope(
            artifact_id=artifact_id,
            artifact_type="control-plane-fixture",
            schema_version="fixture-v1",
            content_hash=content_sha256,
            producer=GATE_PRODUCER,
            inputs=(),
        )
    )


def outcome_of(name: str, result: str, detail: str = "") -> OperationOutcome:
    return OperationOutcome(name=name, result=result, detail=detail)


def call_op(
    name: str,
    action: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> OperationOutcome:
    try:
        action(*args, **kwargs)
        return outcome_of(name, "ok")
    except (StoreRefusalError, StateStoreError, LeaseError) as error:
        return outcome_of(name, error.code, str(error))
    except Exception as error:  # noqa: BLE001 (raw evidence keeps the type name)
        return outcome_of(name, type(error).__name__, str(error))


def fresh_dir(evidence: Path, fixture_id: str) -> Path:
    work = evidence / "scenarios" / fixture_id
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    return work


def write_observation(observation: GateObservation, evidence: Path) -> Path:
    target = evidence / "scenarios" / observation.fixture_id / "observation.json"
    atomic_write(target, canonical_json_bytes(observation))
    return target


def make_observation(
    *,
    fixture_id: str,
    kind: str,
    work: Path,
    operations: list[OperationOutcome],
    fields: dict[str, str] | None = None,
) -> GateObservation:
    return GateObservation(
        fixture_id=fixture_id,
        kind=kind,
        work_dir=str(work),
        operations=tuple(operations),
        fields=fields or {},
    )


__all__ = [
    "GATE_PRODUCER",
    "call_op",
    "fresh_dir",
    "intent_for",
    "make_observation",
    "outcome_of",
    "write_observation",
]
