"""Numbered SQL migrations for the job runner runtime-state database.

``MIGRATIONS`` is the single source of schema truth. Every applied
migration is recorded in ``schema_migrations`` with the sha256 of its
canonical source, so an unknown version or a tampered record refuses to
open. All pending migrations apply inside ONE transaction (SQLite DDL
is transactional), so a migration interrupted mid-way rolls back and
leaves the database at the PREVIOUS version.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Self

from pydantic import Field

from services.contracts.primitives import StrictModel


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
        name="core-runtime-state",
        statements=(
            """CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                sha256 TEXT NOT NULL
            )""",
            """CREATE TABLE state_clock (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                next_seq INTEGER NOT NULL
            )""",
            "INSERT INTO state_clock (id, next_seq) VALUES (1, 1)",
            """CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL,
                current_stage TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at_seq INTEGER NOT NULL,
                updated_at_seq INTEGER NOT NULL
            )""",
            """CREATE TABLE stage_runs (
                job_id TEXT NOT NULL REFERENCES jobs (job_id),
                stage_name TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                input_artifact_hashes TEXT NOT NULL,
                adopted_artifact_hash TEXT,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL,
                last_error_code TEXT,
                PRIMARY KEY (job_id, idempotency_key)
            )""",
            """CREATE TABLE leases (
                resource TEXT PRIMARY KEY,
                holder TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )""",
        ),
    ),
    Migration(
        version=2,
        name="approvals-and-cache-pointers",
        statements=(
            """CREATE TABLE approval_refs (
                purpose TEXT NOT NULL,
                target_hash TEXT NOT NULL,
                artifact_ref TEXT NOT NULL,
                recorded_seq INTEGER NOT NULL UNIQUE,
                PRIMARY KEY (purpose, target_hash, artifact_ref)
            )""",
            """CREATE TABLE cache_pointers (
                cache_key TEXT PRIMARY KEY,
                artifact_hash TEXT NOT NULL,
                producer_version TEXT NOT NULL
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


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def read_applied(connection: sqlite3.Connection) -> Mapping[int, str]:
    """Return applied ``version -> migration sha256`` records."""

    if not _table_exists(connection, "schema_migrations"):
        return {}
    rows = connection.execute(
        "SELECT version, sha256 FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {int(row[0]): str(row[1]) for row in rows}


def applied_version(connection: sqlite3.Connection) -> int:
    applied = read_applied(connection)
    return max(applied) if applied else 0


def _validate(applied: Mapping[int, str], migrations: tuple[Migration, ...]) -> None:
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
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> tuple[Migration, ...]:
    """Validate applied history, then apply pending migrations atomically.

    The whole pending set (DDL + the ``schema_migrations`` inserts) runs
    inside ONE ``BEGIN IMMEDIATE`` transaction; any fault rolls back to
    the previous version. Returns the migrations applied by this call.
    """

    applied = read_applied(connection)
    _validate(applied, migrations)
    pending = migrations[len(applied) :]
    if not pending:
        return ()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for migration in pending:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, sha256) VALUES (?, ?, ?)",
                (migration.version, migration.name, migration_sha256(migration)),
            )
        connection.execute("COMMIT")
    except Exception as error:
        connection.execute("ROLLBACK")
        raise MigrationError(
            "migration-failed",
            f"migration transaction rolled back; database stays at version "
            f"{max(applied) if applied else 0}: {error}",
        ) from error
    return pending


__all__ = [
    "LATEST_VERSION",
    "MIGRATIONS",
    "Migration",
    "MigrationError",
    "applied_version",
    "apply_migrations",
    "migration_sha256",
    "read_applied",
]
