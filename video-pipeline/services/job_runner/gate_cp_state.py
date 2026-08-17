"""Real Todo-9/10 state-machine drivers for the Control Plane gate.

Three drives, all against the production ``StateStore``/CAS/lanes:
stale State-vs-Artifact disagreement (verify fails closed), lease
expiry across the SQL-lane lease rows, and serialized authority (two
writers with the same expectation: exactly one commits, the loser is
``superseded`` without writing, and an identical replay is a no-op).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from services.job_runner.cas import CasError, apply_transition
from services.job_runner.gate_cp_models import OperationOutcome
from services.job_runner.lanes import StateLane
from services.job_runner.state_integrity import verify_against_store
from services.job_runner.state_store import StateStore

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry
    from services.artifact_store.store import ArtifactStore


def synthetic_hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _outcome(name: str, result: str, detail: str = "") -> OperationOutcome:
    return OperationOutcome(name=name, result=result, detail=detail)


def _code_of(name: str, action: Callable[[], object]) -> OperationOutcome:
    try:
        action()
        return _outcome(name, "ok")
    except CasError as error:
        return _outcome(name, error.code, str(error))
    except Exception as error:  # noqa: BLE001 (raw evidence keeps code/type)
        code = getattr(error, "code", type(error).__name__)
        return _outcome(name, str(code), str(error))


def drive_stale_state_disagreement(
    state_dir: Path,
    store: ArtifactStore,
    registry: ArtifactRegistry,
    sha: str,
) -> OperationOutcome:
    """Adopt ``sha`` in the DB, then verify against tampered store bytes."""

    state_dir.mkdir(parents=True, exist_ok=True)
    with StateStore.open(state_dir / "state.sqlite3") as state:
        state.create_job(
            job_id="cp-stale-job",
            episode_id="cp-stale-episode",
            current_stage="ingest",
        )
        apply_transition(
            state,
            "cp-stale-job",
            expected_status="CREATED",
            expected_parent_hash=None,
            new_status="INGESTED",
            new_artifact_hash=sha,
        )
        return _code_of(
            "state-verify-vs-store",
            lambda: verify_against_store(state, store, registry),
        )


def drive_lease_expiry_lanes(state_dir: Path) -> list[OperationOutcome]:
    """Expired lease holder refused; a new holder commits via Todo-10 lanes."""

    first = synthetic_hash("cp-lease-lane-first")
    second = synthetic_hash("cp-lease-lane-second")
    state_dir.mkdir(parents=True, exist_ok=True)
    with StateStore.open(state_dir / "state.sqlite3") as state:
        state.create_job(
            job_id="cp-lease-job",
            episode_id="cp-lease-episode",
            current_stage="ingest",
        )
        now = 1_000_000
        ttl = 60
        holder_a = StateLane(state, "cp-lease-job", holder="holder-a")
        holder_a.acquire(now=now, ttl_seconds=ttl)
        outcomes = [
            _code_of(
                "lane-holder-a-apply",
                lambda: holder_a.apply(
                    expected_status="CREATED",
                    expected_parent_hash=None,
                    new_status="INGESTED",
                    new_artifact_hash=first,
                    now=now,
                ),
            )
        ]
        expired = now + ttl + 1
        outcomes.append(
            _code_of(
                "lane-expired-holder-refused",
                lambda: holder_a.apply(
                    expected_status="INGESTED",
                    expected_parent_hash=first,
                    new_status="NORMALIZED",
                    new_artifact_hash=second,
                    now=expired,
                ),
            )
        )
        holder_b = StateLane(state, "cp-lease-job", holder="holder-b")
        holder_b.acquire(now=expired, ttl_seconds=ttl)
        outcomes.append(
            _code_of(
                "lane-new-holder-commit",
                lambda: holder_b.apply(
                    expected_status="INGESTED",
                    expected_parent_hash=first,
                    new_status="NORMALIZED",
                    new_artifact_hash=second,
                    now=expired,
                ),
            )
        )
        return outcomes


def drive_serialized_authority(state_dir: Path) -> list[OperationOutcome]:
    """Two writers, one expectation: exactly one commits; loser superseded."""

    first = synthetic_hash("cp-serial-first")
    second = synthetic_hash("cp-serial-second")
    state_dir.mkdir(parents=True, exist_ok=True)
    with StateStore.open(state_dir / "state.sqlite3") as state:
        state.create_job(
            job_id="cp-serial-job",
            episode_id="cp-serial-episode",
            current_stage="ingest",
        )

        def writer_one() -> None:
            apply_transition(
                state,
                "cp-serial-job",
                expected_status="CREATED",
                expected_parent_hash=None,
                new_status="INGESTED",
                new_artifact_hash=first,
            )

        def writer_two() -> None:
            apply_transition(
                state,
                "cp-serial-job",
                expected_status="CREATED",
                expected_parent_hash=None,
                new_status="INGESTED",
                new_artifact_hash=second,
            )

        def replay() -> None:
            apply_transition(
                state,
                "cp-serial-job",
                expected_status="INGESTED",
                expected_parent_hash=first,
                new_status="INGESTED",
                new_artifact_hash=first,
            )

        return [
            _code_of("cas-writer-one-commits", writer_one),
            _code_of("cas-writer-two-superseded", writer_two),
            _code_of("cas-idempotent-replay", replay),
        ]


__all__ = [
    "drive_lease_expiry_lanes",
    "drive_serialized_authority",
    "drive_stale_state_disagreement",
    "synthetic_hash",
]
