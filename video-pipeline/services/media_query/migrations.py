"""Numbered DuckDB migrations for the media-query index (Todo 9 discipline).

``MIGRATIONS`` is the single source of schema truth: every applied migration
is recorded in ``schema_migrations`` with the sha256 of its canonical source,
so an unknown version or a tampered record refuses to open. Each pending
migration applies inside ONE explicit transaction, so an interruption rolls
back and leaves the database at the PREVIOUS version.

The DuckDB file is a REBUILDABLE SEARCH INDEX ONLY — file artifacts stay
canonical; index determinism is asserted at ROW level, never file bytes.
``FROZEN_ANALYZER_VERSIONS`` is the closed producer allowlist the indexer
enforces: an analyzer artifact from any other producer version is rejected,
never silently mixed into the index.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Self

import duckdb
from pydantic import Field

from services.contracts.primitives import StrictModel

MEDIA_DB_NAME: Final = "media.duckdb"

FROZEN_ANALYZER_VERSIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "asr-whisper-cpp": "todo33-v1",
        "analyze-dialogue": "todo34-v1",
        "analyze-visual": "todo35-v1",
    }
)


class Migration(StrictModel):
    version: int = Field(ge=1, strict=True)
    name: str
    statements: tuple[str, ...]


def migration_sha256(migration: Migration) -> str:
    payload = json.dumps(
        {
            "version": migration.version,
            "name": migration.name,
            "statements": list(migration.statements),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="media-index-core",
        statements=(
            """CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, sha256 TEXT NOT NULL
            )""",
            """CREATE TABLE sources (
                source_id TEXT NOT NULL, path TEXT, sha256 TEXT NOT NULL,
                sample_rate INTEGER, channels INTEGER, codec TEXT,
                width INTEGER, height INTEGER, rate_num INTEGER, rate_den INTEGER,
                frame_count INTEGER, analyzer_version TEXT NOT NULL,
                artifact_sha TEXT NOT NULL, PRIMARY KEY (source_id, artifact_sha)
            )""",
            """CREATE TABLE transcript_segments (
                source_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
                start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT NOT NULL,
                confidence INTEGER, analyzer_version TEXT NOT NULL,
                artifact_sha TEXT NOT NULL, PRIMARY KEY (source_id, segment_index, artifact_sha)
            )""",
            """CREATE TABLE sample_spans (
                source_id TEXT NOT NULL, kind TEXT NOT NULL, segment_index INTEGER NOT NULL,
                start_sample BIGINT NOT NULL, end_sample BIGINT NOT NULL,
                sample_rate INTEGER NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
                text TEXT NOT NULL, experimental BOOLEAN NOT NULL,
                analyzer_version TEXT NOT NULL, artifact_sha TEXT NOT NULL,
                PRIMARY KEY (source_id, kind, segment_index, start_sample, artifact_sha)
            )""",
            """CREATE TABLE silence_ranges (
                source_id TEXT NOT NULL, kind TEXT NOT NULL,
                start_sample BIGINT NOT NULL, end_sample BIGINT NOT NULL,
                sample_rate INTEGER NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
                confidence INTEGER, rule_id TEXT, analyzer_version TEXT NOT NULL,
                artifact_sha TEXT NOT NULL,
                PRIMARY KEY (source_id, kind, start_sample, end_sample, artifact_sha)
            )""",
            """CREATE TABLE quality_ranges (
                source_id TEXT NOT NULL, kind TEXT NOT NULL,
                start_frame INTEGER NOT NULL, end_frame INTEGER NOT NULL,
                confidence INTEGER NOT NULL, rule_id TEXT NOT NULL,
                analyzer_version TEXT NOT NULL, artifact_sha TEXT NOT NULL,
                PRIMARY KEY (source_id, kind, start_frame, end_frame, artifact_sha)
            )""",
            """CREATE TABLE contact_refs (
                source_id TEXT NOT NULL, sheet_path TEXT NOT NULL,
                sheet_sha256 TEXT NOT NULL, frame_indexes INTEGER[],
                cols INTEGER NOT NULL, rows INTEGER NOT NULL,
                thumb_w INTEGER NOT NULL, thumb_h INTEGER NOT NULL,
                generator TEXT NOT NULL, cadence_frames INTEGER NOT NULL,
                analyzer_version TEXT NOT NULL, artifact_sha TEXT NOT NULL,
                PRIMARY KEY (source_id, sheet_sha256, artifact_sha)
            )""",
            """CREATE TABLE statistics (
                source_id TEXT NOT NULL, metric TEXT NOT NULL, value BIGINT NOT NULL,
                unit TEXT NOT NULL, analyzer_version TEXT NOT NULL,
                artifact_sha TEXT NOT NULL, PRIMARY KEY (source_id, metric, artifact_sha)
            )""",
            """CREATE TABLE lineage (
                artifact_id TEXT NOT NULL, artifact_type TEXT NOT NULL,
                schema_version TEXT NOT NULL, artifact_sha TEXT NOT NULL,
                producer_name TEXT NOT NULL, analyzer_version TEXT NOT NULL,
                row_count BIGINT NOT NULL, PRIMARY KEY (artifact_sha)
            )""",
        ),
    ),
)

LATEST_VERSION = MIGRATIONS[-1].version


class MigrationError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

    @classmethod
    def unknown_version(cls, version: int) -> Self:
        return cls(
            "unknown-schema-version",
            f"database schema version {version} is newer or unknown to this code "
            f"(latest known: {LATEST_VERSION}); refusing to open",
        )


def _table_exists(connection: duckdb.DuckDBPyConnection, name: str) -> bool:
    row = connection.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = ?",
        [name],
    ).fetchone()
    return row is not None and int(row[0]) > 0


def read_applied(connection: duckdb.DuckDBPyConnection) -> Mapping[int, str]:
    if not _table_exists(connection, "schema_migrations"):
        return {}
    rows = connection.execute(
        "SELECT version, sha256 FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {int(row[0]): str(row[1]) for row in rows}


def validate_applied(
    applied: Mapping[int, str], migrations: tuple[Migration, ...]
) -> None:
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise MigrationError(
            "migration-list-invalid",
            f"migration versions must be 1..N in order, got {versions}",
        )
    known = {migration.version: migration for migration in migrations}
    for version, recorded_sha256 in applied.items():
        migration = known.get(version)
        if migration is None:
            raise MigrationError.unknown_version(version)
        expected = migration_sha256(migration)
        if recorded_sha256 != expected:
            raise MigrationError(
                "migration-hash-mismatch",
                f"migration {version} recorded sha256 {recorded_sha256} does not "
                f"match its source hash {expected}; schema_migrations is tampered "
                "or this code disagrees with the applied schema",
            )
    if sorted(applied) != list(range(1, len(applied) + 1)):
        raise MigrationError(
            "schema-gap",
            f"applied migration versions must be a contiguous prefix, got {sorted(applied)}",
        )


def apply_migrations(
    connection: duckdb.DuckDBPyConnection,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> tuple[Migration, ...]:
    applied = read_applied(connection)
    validate_applied(applied, migrations)
    pending = migrations[len(applied) :]
    if not pending:
        return ()
    connection.execute("BEGIN TRANSACTION")
    try:
        for migration in pending:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, sha256) VALUES (?, ?, ?)",
                [migration.version, migration.name, migration_sha256(migration)],
            )
        connection.execute("COMMIT")
    except BaseException as error:
        connection.execute("ROLLBACK")
        raise MigrationError(
            "migration-failed",
            f"migration transaction rolled back; database stays at version "
            f"{max(applied) if applied else 0}: {error}",
        ) from error
    return pending


__all__ = [
    "FROZEN_ANALYZER_VERSIONS",
    "LATEST_VERSION",
    "MEDIA_DB_NAME",
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "apply_migrations",
    "migration_sha256",
    "read_applied",
    "validate_applied",
]
