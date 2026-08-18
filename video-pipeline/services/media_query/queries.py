"""Bounded read-only queries over the media-query index.

Every query is a fixed SQL string with ``?`` placeholders for ALL caller
values — an adversarial value such as ``"'; DROP TABLE x; --"`` is bound as
DATA and can never alter the statement. All spans are half-open
``[start, end)`` and overlap is strict (``start < query_end AND end >
query_start``). Rows carry their lineage (analyzer version + artifact hash)
so callers can resolve every fact back to its canonical artifact.
"""

from __future__ import annotations

import duckdb

from services.media_query.rows import (
    ContactRefRow,
    QualityRangeRow,
    SilenceRangeRow,
    StatisticRow,
    TranscriptSegmentRow,
)


def _require_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise TypeError(f"index column must hold an int, got {type(value).__name__}")


def _optional_int(value: object) -> int | None:
    return None if value is None else _require_int(value)


def _require_str(value: object) -> str:
    if isinstance(value, str):
        return value
    raise TypeError(f"index column must hold a str, got {type(value).__name__}")


def _optional_str(value: object) -> str | None:
    return None if value is None else _require_str(value)


def find_transcript_segments(
    connection: duckdb.DuckDBPyConnection,
    source_id: str,
    start_ms: int,
    end_ms: int,
) -> tuple[TranscriptSegmentRow, ...]:
    rows = connection.execute(
        "SELECT source_id, segment_index, start_ms, end_ms, text, confidence, "
        "analyzer_version, artifact_sha FROM transcript_segments "
        "WHERE source_id = ? AND start_ms < ? AND end_ms > ? "
        "ORDER BY segment_index, artifact_sha",
        [source_id, end_ms, start_ms],
    ).fetchall()
    return tuple(
        TranscriptSegmentRow(
            source_id=_require_str(row[0]),
            segment_index=_require_int(row[1]),
            start_ms=_require_int(row[2]),
            end_ms=_require_int(row[3]),
            text=_require_str(row[4]),
            confidence=_optional_int(row[5]),
            analyzer_version=_require_str(row[6]),
            artifact_sha=_require_str(row[7]),
        )
        for row in rows
    )


def find_silence_ranges(
    connection: duckdb.DuckDBPyConnection,
    source_id: str,
    start_sample: int,
    end_sample: int,
) -> tuple[SilenceRangeRow, ...]:
    rows = connection.execute(
        "SELECT source_id, kind, start_sample, end_sample, sample_rate, start_ms, "
        "end_ms, confidence, rule_id, analyzer_version, artifact_sha "
        "FROM silence_ranges "
        "WHERE source_id = ? AND start_sample < ? AND end_sample > ? "
        "ORDER BY kind, start_sample, end_sample, artifact_sha",
        [source_id, end_sample, start_sample],
    ).fetchall()
    return tuple(
        SilenceRangeRow(
            source_id=_require_str(row[0]),
            kind=_require_str(row[1]),
            start_sample=_require_int(row[2]),
            end_sample=_require_int(row[3]),
            sample_rate=_require_int(row[4]),
            start_ms=_require_int(row[5]),
            end_ms=_require_int(row[6]),
            confidence=_optional_int(row[7]),
            rule_id=_optional_str(row[8]),
            analyzer_version=_require_str(row[9]),
            artifact_sha=_require_str(row[10]),
        )
        for row in rows
    )


def find_quality_ranges(
    connection: duckdb.DuckDBPyConnection,
    source_id: str,
    start_frame: int,
    end_frame: int,
    *,
    kind: str | None = None,
) -> tuple[QualityRangeRow, ...]:
    rows = connection.execute(
        "SELECT source_id, kind, start_frame, end_frame, confidence, rule_id, "
        "analyzer_version, artifact_sha FROM quality_ranges "
        "WHERE source_id = ? AND start_frame < ? AND end_frame > ? AND "
        "(? IS NULL OR kind = ?) "
        "ORDER BY kind, start_frame, end_frame, artifact_sha",
        [source_id, end_frame, start_frame, kind, kind],
    ).fetchall()
    return tuple(
        QualityRangeRow(
            source_id=_require_str(row[0]),
            kind=_require_str(row[1]),
            start_frame=_require_int(row[2]),
            end_frame=_require_int(row[3]),
            confidence=_require_int(row[4]),
            rule_id=_require_str(row[5]),
            analyzer_version=_require_str(row[6]),
            artifact_sha=_require_str(row[7]),
        )
        for row in rows
    )


def get_contact_refs(
    connection: duckdb.DuckDBPyConnection,
    source_id: str,
) -> tuple[ContactRefRow, ...]:
    rows = connection.execute(
        "SELECT source_id, sheet_path, sheet_sha256, frame_indexes, cols, rows, "
        "thumb_w, thumb_h, generator, cadence_frames, analyzer_version, "
        "artifact_sha FROM contact_refs "
        "WHERE source_id = ? "
        "ORDER BY sheet_sha256, artifact_sha",
        [source_id],
    ).fetchall()
    return tuple(
        ContactRefRow(
            source_id=_require_str(row[0]),
            sheet_path=_require_str(row[1]),
            sheet_sha256=_require_str(row[2]),
            frame_indexes=tuple(_require_int(frame) for frame in row[3]),
            cols=_require_int(row[4]),
            rows=_require_int(row[5]),
            thumb_w=_require_int(row[6]),
            thumb_h=_require_int(row[7]),
            generator=_require_str(row[8]),
            cadence_frames=_require_int(row[9]),
            analyzer_version=_require_str(row[10]),
            artifact_sha=_require_str(row[11]),
        )
        for row in rows
    )


def get_statistics(
    connection: duckdb.DuckDBPyConnection,
    source_id: str,
) -> tuple[StatisticRow, ...]:
    rows = connection.execute(
        "SELECT source_id, metric, value, unit, analyzer_version, artifact_sha "
        "FROM statistics WHERE source_id = ? "
        "ORDER BY metric, artifact_sha",
        [source_id],
    ).fetchall()
    return tuple(
        StatisticRow(
            source_id=_require_str(row[0]),
            metric=_require_str(row[1]),
            value=_require_int(row[2]),
            unit=_require_str(row[3]),
            analyzer_version=_require_str(row[4]),
            artifact_sha=_require_str(row[5]),
        )
        for row in rows
    )


__all__ = [
    "find_quality_ranges",
    "find_silence_ranges",
    "find_transcript_segments",
    "get_contact_refs",
    "get_statistics",
]
