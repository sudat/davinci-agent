"""DuckDB media-query index over registered analyzer artifacts.

The index is REBUILDABLE, never canonical: only registered artifacts whose
bytes re-verify in the content-addressed store are indexed, and every row
carries the ``artifact_sha`` of its parent artifact, so no canonical-only
row-set can exist. Determinism contract: rebuilding from the same artifacts
yields IDENTICAL ORDERED ROW SETS (asserted row-level); DuckDB file BYTES are
not claimed deterministic and are never treated as evidence.

Every mutation (full ``rebuild`` or incremental ``upsert_analyzer_artifact``)
runs inside ONE explicit ``BEGIN``/``COMMIT``; any fault triggers ``ROLLBACK``
and leaves the index at its previous committed state.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Self

import duckdb
from pydantic import ValidationError

from services.analyze.asr_models import TranscriptArtifact
from services.analyze.candidate_models import AnalysisArtifact
from services.analyze.visual_models import VisualAnalysisArtifact
from services.artifact_store.store import ArtifactStore, StoreRefusalError
from services.foundation_io import canonical_model_bytes
from services.media_query.migrations import (
    FROZEN_ANALYZER_VERSIONS,
    MEDIA_DB_NAME,
    MIGRATIONS,
    MigrationError,
    apply_migrations,
    read_applied,
    validate_applied,
)
from services.media_query.rows import (
    AnalyzerArtifact,
    lineage_row,
    rows_for_artifact,
)
from services.media_query.writer import (
    ALL_TABLES,
    delete_artifact_rows,
    write_row_set,
)

if TYPE_CHECKING:
    from services.artifact_registry.registry import ArtifactRegistry

INDEXABLE_TYPES: dict[str, type[AnalyzerArtifact]] = {
    "transcript_asr_whisper_cpp": TranscriptArtifact,
    "analysis_dialogue_candidates": AnalysisArtifact,
    "analysis_visual_minimum": VisualAnalysisArtifact,
}


class MediaQueryError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def artifact_content_sha256(artifact: AnalyzerArtifact) -> str:
    return hashlib.sha256(canonical_model_bytes(artifact)).hexdigest()


def _check_supported(artifact: AnalyzerArtifact) -> None:
    frozen = FROZEN_ANALYZER_VERSIONS.get(artifact.producer.name)
    if frozen is None or artifact.producer.version != frozen:
        raise MediaQueryError(
            "unsupported-analyzer-version",
            f"producer {artifact.producer.name} version {artifact.producer.version} "
            "is not in the frozen analyzer allowlist; refusing to index",
        )


def insert_artifact_rows(
    connection: duckdb.DuckDBPyConnection,
    artifact: AnalyzerArtifact,
    artifact_sha: str,
) -> None:
    """Insert one artifact's row-set (idempotent per artifact_sha) plus lineage."""

    _check_supported(artifact)
    row_set = rows_for_artifact(artifact, artifact_sha)
    delete_artifact_rows(connection, artifact_sha)
    write_row_set(connection, row_set)
    lineage = lineage_row(artifact, artifact_sha, row_set.row_count())
    connection.execute(
        "INSERT INTO lineage VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            lineage.artifact_id,
            lineage.artifact_type,
            lineage.schema_version,
            lineage.artifact_sha,
            lineage.producer_name,
            lineage.analyzer_version,
            lineage.row_count,
        ],
    )


def collect_registered_artifacts(
    store: ArtifactStore, registry: ArtifactRegistry
) -> tuple[tuple[AnalyzerArtifact, str], ...]:
    """Load every registered, hash-verified, indexable analyzer artifact."""

    collected: list[tuple[AnalyzerArtifact, str]] = []
    entries = sorted(
        registry.load().entries.values(), key=lambda entry: (entry.sequence, entry.artifact_id)
    )
    for entry in entries:
        model_cls = INDEXABLE_TYPES.get(entry.artifact_type)
        if model_cls is None:
            continue
        try:
            payload, _object_ref = store.reopen(entry.content_sha256)
        except StoreRefusalError as error:
            raise MediaQueryError(error.code, error.detail) from error
        try:
            artifact = model_cls.model_validate_json(payload)
        except ValidationError as error:
            raise MediaQueryError(
                "artifact-parse-mismatch",
                f"registered artifact {entry.artifact_id} does not parse as "
                f"{entry.artifact_type}: {error}",
            ) from error
        payload_envelope = (
            artifact.artifact_id, artifact.artifact_type, artifact.schema_version,
            artifact.producer.name, artifact.producer.version)
        entry_envelope = (
            entry.artifact_id, entry.artifact_type, entry.schema_version,
            entry.producer.name, entry.producer.version)
        if payload_envelope != entry_envelope:
            raise MediaQueryError(
                "registry-drift",
                f"artifact {entry.artifact_id} payload disagrees with its registry entry",
            )
        collected.append((artifact, entry.content_sha256))
    return tuple(collected)


class MediaQueryIndex:
    """Context-managed connection over one episode's ``media.duckdb``."""

    def __init__(
        self, connection: duckdb.DuckDBPyConnection, path: Path, *, read_only: bool
    ) -> None:
        self._connection = connection
        self._path = path
        self._read_only = read_only
        self._closed = False

    @classmethod
    def open(cls, path: Path, *, read_only: bool = False) -> Self:
        try:
            if read_only:
                connection = duckdb.connect(str(path), read_only=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                connection = duckdb.connect(str(path))
        except duckdb.Error as error:
            raise MediaQueryError("index-unreadable", str(error)) from error
        if read_only:
            applied = read_applied(connection)
            if not applied:
                raise MigrationError(
                    "unknown-schema-version",
                    "read-only open requires an already-migrated index",
                )
            validate_applied(applied, MIGRATIONS)
        else:
            apply_migrations(connection)
        return cls(connection, path, read_only=read_only)

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._connection

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _run_transaction(self, action: Callable[[], None]) -> None:
        if self._read_only:
            raise MediaQueryError(
                "read-only-index", "this index connection was opened read_only"
            )
        self._connection.execute("BEGIN TRANSACTION")
        try:
            action()
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def rebuild(
        self, store: ArtifactStore, registry: ArtifactRegistry, episode_dir: Path
    ) -> None:
        expected = episode_dir / MEDIA_DB_NAME
        if not episode_dir.is_dir() or expected.resolve() != self._path.resolve():
            raise MediaQueryError(
                "episode-layout-mismatch",
                f"index path must be {expected} (PRD episode layout: media.duckdb "
                f"at the episode root), got {self._path}",
            )
        collected = collect_registered_artifacts(store, registry)

        def apply() -> None:
            for table in ALL_TABLES:
                # table names come from the frozen ALL_TABLES constant, never callers
                self._connection.execute(f"DELETE FROM {table}")  # noqa: S608
            for artifact, artifact_sha in collected:
                insert_artifact_rows(self._connection, artifact, artifact_sha)

        self._run_transaction(apply)

    def upsert_analyzer_artifact(self, artifact: AnalyzerArtifact) -> None:
        artifact_sha = artifact_content_sha256(artifact)

        def apply() -> None:
            insert_artifact_rows(self._connection, artifact, artifact_sha)

        self._run_transaction(apply)


__all__ = [
    "INDEXABLE_TYPES",
    "MEDIA_DB_NAME",
    "MediaQueryError",
    "MediaQueryIndex",
    "artifact_content_sha256",
    "collect_registered_artifacts",
    "insert_artifact_rows",
]
