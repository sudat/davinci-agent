"""Transactional row writers for the media-query index.

Every insert is parameterized: the only string-built statement text uses
table names from the frozen ``DATA_TABLES`` constant (never caller values).
``write_row_set`` and ``delete_artifact_rows`` always run inside one explicit
caller-managed transaction. Row models mirror their table column order by
construction, so ``model_dump()`` values bind positionally in insert order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb

if TYPE_CHECKING:
    from services.contracts.primitives import StrictModel
    from services.media_query.rows import IndexRowSet

DATA_TABLES: tuple[str, ...] = (
    "sources",
    "transcript_segments",
    "sample_spans",
    "silence_ranges",
    "quality_ranges",
    "contact_refs",
    "statistics",
)

ALL_TABLES: tuple[str, ...] = (*DATA_TABLES, "lineage")


def delete_artifact_rows(connection: duckdb.DuckDBPyConnection, artifact_sha: str) -> None:
    for table in ALL_TABLES:
        # table names come from the frozen ALL_TABLES constant, never callers
        connection.execute(f"DELETE FROM {table} WHERE artifact_sha = ?", [artifact_sha])  # noqa: S608


def write_row_set(connection: duckdb.DuckDBPyConnection, row_set: IndexRowSet) -> None:
    for table in DATA_TABLES:
        rows: tuple[StrictModel, ...] = getattr(row_set, table)
        if not rows:
            continue
        columns = rows[0].model_dump()
        # table/column names come from the frozen constants and row models,
        # never caller values
        column_list = ", ".join(columns)
        placeholders = ", ".join(["?"] * len(columns))
        sql = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"  # noqa: S608
        connection.executemany(sql, [list(row.model_dump().values()) for row in rows])


__all__ = [
    "ALL_TABLES",
    "DATA_TABLES",
    "delete_artifact_rows",
    "write_row_set",
]
