from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from services.artifact_registry.registry import ArtifactRegistry
from services.artifact_store.models import PublicationIntent, PublicationReceipt
from services.artifact_store.store import ArtifactStore
from services.contracts.primitives import (
    ArtifactEnvelope,
    ArtifactId,
    Producer,
    Sha256,
)
from services.job_runner.migrations import applied_version
from services.job_runner.state_models import StageRunRow, StageRunStatus

DEFAULT_PRODUCER = Producer(name="job-runner-test-producer", version="v1")


def make_artifact_store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifact-store")


def make_registry(tmp_path: Path) -> ArtifactRegistry:
    return ArtifactRegistry(tmp_path / "registry")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def intent_for(artifact_id: str, payload: bytes) -> PublicationIntent:
    return PublicationIntent(
        envelope=ArtifactEnvelope(
            artifact_id=artifact_id,
            artifact_type="job-runner-test-artifact",
            schema_version="job-runner-test-v1",
            content_hash=sha256_bytes(payload),
            producer=DEFAULT_PRODUCER,
            inputs=(),
        )
    )


def publish(
    store: ArtifactStore,
    artifact_id: str,
    payload: bytes,
) -> PublicationReceipt:
    return store.publish(intent_for(artifact_id, payload), payload)


def publish_and_register(
    store: ArtifactStore,
    registry: ArtifactRegistry,
    artifact_id: str,
    payload: bytes,
) -> PublicationReceipt:
    receipt = publish(store, artifact_id, payload)
    registry.register(store, receipt)
    return receipt


def make_stage_run(
    *,
    stage_name: str,
    idempotency_key: str,
    input_artifact_hashes: tuple[str, ...] = (),
    adopted_artifact_hash: str | None = None,
    status: StageRunStatus = "succeeded",
) -> StageRunRow:
    return StageRunRow(
        job_id="job-1",
        stage_name=stage_name,
        input_artifact_hashes=tuple(input_artifact_hashes),
        adopted_artifact_hash=adopted_artifact_hash,
        status=status,
        idempotency_key=idempotency_key,
    )


def open_raw(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(db_path, isolation_level=None)


def read_schema_version(db_path: Path) -> int:
    connection = open_raw(db_path)
    try:
        return applied_version(connection)
    finally:
        connection.close()


def table_names(db_path: Path) -> set[str]:
    connection = open_raw(db_path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


def table_columns(db_path: Path) -> dict[str, list[str]]:
    connection = open_raw(db_path)
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        columns = {
            table: [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
            for table in sorted(tables)
        }
    finally:
        connection.close()
    return columns


def artifact_ref(*, artifact_id: ArtifactId, sha: Sha256) -> tuple[str, str]:
    return artifact_id, sha
