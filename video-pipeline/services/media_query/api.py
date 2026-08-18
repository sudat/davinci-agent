"""MediaQueryApi — the only public read-only media-query surface (PRD 10.3).

Exactly the seven Phase-1 methods of ``FROZEN_METHOD_ALLOWLIST``;
Review-Example / Review-Learning / past-Plan retrieval is a Phase-5
concern and deliberately absent. The Editorial model gets no raw DB
handle, no arbitrary SQL, no shell, no write path, no filesystem
paths, no network endpoints, and no unrestricted frame dump: requests
are typed ``api_models`` models; every statement is FIXED module text
with ``?``-bound data (Todo-36 discipline; text search is DuckDB
``contains`` — exact-substring, so SQL-looking strings are literal
text, never executed); reads are bounded by FROZEN_MAX_PAGE_SIZE /
FROZEN_ROW_BUDGET; the connection is name-mangled private behind
``__slots__``; range statistics are deterministic in-code aggregates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Self

import duckdb

from services.media_query import api_models as m
from services.media_query.index import MediaQueryIndex
from services.media_query.queries import (
    _optional_int,
    _optional_str,
    find_quality_ranges,
    find_silence_ranges,
    get_contact_refs,
)
from services.media_query.queries import (
    _require_int as _int,
)
from services.media_query.queries import (
    _require_str as _str,
)

_SQL_EPISODE_SUMMARY: Final = (
    "SELECT source_id, sha256, sample_rate, channels, codec, width, height, "
    "rate_num, rate_den, frame_count, analyzer_version, artifact_sha "
    "FROM sources WHERE source_id = ? ORDER BY artifact_sha")
_SQL_TRANSCRIPT_COUNT: Final = (
    "SELECT count(*) FROM transcript_segments WHERE source_id = ? AND contains(text, ?)"
)
_SQL_TRANSCRIPT_PAGE: Final = (
    "SELECT source_id, segment_index, start_ms, end_ms, text, confidence, analyzer_version, "
    "artifact_sha FROM transcript_segments WHERE source_id = ? AND contains(text, ?) "
    "ORDER BY segment_index, artifact_sha LIMIT ? OFFSET ?"
)
_SQL_SILENCE_COUNT: Final = (
    "SELECT count(*) FROM silence_ranges "
    "WHERE source_id = ? AND start_sample < ? AND end_sample > ?"
)
_SQL_SILENCE_PAGE: Final = (
    "SELECT source_id, kind, start_sample, end_sample, sample_rate, confidence, "
    "analyzer_version, artifact_sha FROM silence_ranges WHERE source_id = ? "
    "AND start_sample < ? AND end_sample > ? "
    "ORDER BY kind, start_sample, end_sample, artifact_sha LIMIT ? OFFSET ?"
)
_SQL_QUALITY_COUNT: Final = (
    "SELECT count(*) FROM quality_ranges WHERE source_id = ? AND start_frame < ? AND end_frame > ?"
)
_SQL_QUALITY_PAGE: Final = (
    "SELECT source_id, kind, start_frame, end_frame, confidence, rule_id, analyzer_version, "
    "artifact_sha FROM quality_ranges WHERE source_id = ? AND start_frame < ? AND end_frame > ? "
    "ORDER BY kind, start_frame, end_frame, artifact_sha LIMIT ? OFFSET ?"
)


class MediaQueryApi:
    """Read-only, allowlisted view over one episode's ``media.duckdb``."""

    __slots__ = ("__connection",)

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self.__connection = connection

    @classmethod
    def open(cls, index_path: Path) -> Self:
        return cls(MediaQueryIndex.open(index_path, read_only=True).connection)

    def close(self) -> None:
        self.__connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __fetch(self, sql: str, params: list[object]) -> tuple[tuple[object, ...], ...]:
        return tuple(self.__connection.execute(sql, params).fetchall())

    def __refuse_oversized(self, what: str, count: int) -> None:
        if count > m.FROZEN_ROW_BUDGET:
            raise m.ApiBudgetExceeded(
                f"{what} matched {count} rows; frozen budget is {m.FROZEN_ROW_BUDGET}; "
                "narrow the span or text query")

    def __budgeted_total(self, what: str, sql: str, params: list[object]) -> int:
        row = self.__connection.execute(sql, params).fetchone()
        if row is None:
            raise TypeError("count query returned no rows")
        total = _int(row[0])
        self.__refuse_oversized(what, total)
        return total

    def __page_window(self, what: str, pagination: m.Pagination) -> None:
        if pagination.offset + pagination.limit > m.FROZEN_ROW_BUDGET:
            raise m.ApiBudgetExceeded(
                f"{what} page window [{pagination.offset}, {pagination.offset + pagination.limit})"
                f" exceeds the frozen budget of {m.FROZEN_ROW_BUDGET} rows; narrow the query")

    def episode_summary(self, request: m.EpisodeSummaryRequest) -> m.EpisodeSummaryResponse:
        rows = self.__fetch(_SQL_EPISODE_SUMMARY, [request.source_id])
        self.__refuse_oversized("episode_summary", len(rows))
        return m.EpisodeSummaryResponse(
            total=len(rows),
            rows=tuple(
                m.EpisodeSummaryRow(
                    source_id=_str(row[0]), sha256=_str(row[1]),
                    source_kind="audio" if row[2] is not None else "video",
                    sample_rate=_optional_int(row[2]), channels=_optional_int(row[3]),
                    codec=_optional_str(row[4]), width=_optional_int(row[5]),
                    height=_optional_int(row[6]), rate_num=_optional_int(row[7]),
                    rate_den=_optional_int(row[8]), frame_count=_optional_int(row[9]),
                    analyzer_version=_str(row[10]), artifact_sha=_str(row[11]))
                for row in rows),
        )

    def search_transcripts(
        self, request: m.SearchTranscriptsRequest
    ) -> m.SearchTranscriptsResponse:
        self.__page_window("search_transcripts", request.pagination)
        total = self.__budgeted_total(
            "search_transcripts", _SQL_TRANSCRIPT_COUNT, [request.source_id, request.text_query])
        rows = self.__fetch(_SQL_TRANSCRIPT_PAGE, [
            request.source_id, request.text_query,
            request.pagination.limit, request.pagination.offset])
        return m.SearchTranscriptsResponse(
            total=total,
            rows=tuple(
                m.TranscriptHitRow(
                    source_id=_str(row[0]), segment_index=_int(row[1]),
                    span=m.MsSpan(start_ms=_int(row[2]), end_ms=_int(row[3])),
                    text=_str(row[4]), confidence=_optional_int(row[5]),
                    analyzer_version=_str(row[6]), artifact_sha=_str(row[7]))
                for row in rows),
            limit=request.pagination.limit, offset=request.pagination.offset)

    def silence_ranges(self, request: m.SilenceRangesRequest) -> m.SilenceRangesResponse:
        self.__page_window("silence_ranges", request.pagination)
        window = [request.source_id, request.span.end_sample, request.span.start_sample]
        total = self.__budgeted_total("silence_ranges", _SQL_SILENCE_COUNT, window)
        rows = self.__fetch(
            _SQL_SILENCE_PAGE,
            [*window, request.pagination.limit, request.pagination.offset])
        return m.SilenceRangesResponse(
            total=total,
            rows=tuple(
                m.SilenceHitRow(
                    source_id=_str(row[0]), kind=_str(row[1]),
                    span=m.SampleSpan(start_sample=_int(row[2]), end_sample=_int(row[3])),
                    sample_rate=_int(row[4]), confidence=_optional_int(row[5]),
                    analyzer_version=_str(row[6]), artifact_sha=_str(row[7]))
                for row in rows),
            limit=request.pagination.limit, offset=request.pagination.offset)

    def quality_ranges(self, request: m.QualityRangesRequest) -> m.QualityRangesResponse:
        self.__page_window("quality_ranges", request.pagination)
        window = [request.source_id, request.span.end_frame, request.span.start_frame]
        total = self.__budgeted_total("quality_ranges", _SQL_QUALITY_COUNT, window)
        rows = self.__fetch(
            _SQL_QUALITY_PAGE,
            [*window, request.pagination.limit, request.pagination.offset])
        return m.QualityRangesResponse(
            total=total,
            rows=tuple(
                m.QualityHitRow(
                    source_id=_str(row[0]), kind=_str(row[1]),
                    span=m.FrameSpan(start_frame=_int(row[2]), end_frame=_int(row[3])),
                    confidence=_int(row[4]), rule_id=_str(row[5]),
                    analyzer_version=_str(row[6]), artifact_sha=_str(row[7]))
                for row in rows),
            limit=request.pagination.limit, offset=request.pagination.offset)

    def contact_sheets(self, request: m.ContactSheetsRequest) -> m.ContactSheetsResponse:
        found = get_contact_refs(self.__connection, request.source_id)
        self.__refuse_oversized("contact_sheets", len(found))
        return m.ContactSheetsResponse(
            total=len(found),
            rows=tuple(
                m.ContactSheetRow(
                    source_id=sheet.source_id,
                    span=m.FrameSpan(start_frame=min(sheet.frame_indexes),
                                     end_frame=max(sheet.frame_indexes) + 1),
                    sheet_sha256=sheet.sheet_sha256, frame_indexes=sheet.frame_indexes,
                    cols=sheet.cols, rows=sheet.rows, thumb_w=sheet.thumb_w,
                    thumb_h=sheet.thumb_h, generator=sheet.generator,
                    cadence_frames=sheet.cadence_frames, analyzer_version=sheet.analyzer_version,
                    artifact_sha=sheet.artifact_sha)
                for sheet in found if sheet.frame_indexes),
        )

    def candidate_frames(self, request: m.CandidateFramesRequest) -> m.CandidateFramesResponse:
        frames = [
            m.CandidateFrameRow(
                source_id=sheet.source_id,
                span=m.FrameSpan(start_frame=frame, end_frame=frame + 1),
                frame_index=frame, sheet_sha256=sheet.sheet_sha256,
                analyzer_version=sheet.analyzer_version, artifact_sha=sheet.artifact_sha)
            for sheet in get_contact_refs(self.__connection, request.source_id)
            for frame in sheet.frame_indexes
            if request.span.start_frame <= frame < request.span.end_frame]
        self.__refuse_oversized("candidate_frames", len(frames))
        frames.sort(key=lambda row: (row.frame_index, row.sheet_sha256, row.artifact_sha))
        return m.CandidateFramesResponse(total=len(frames), rows=tuple(frames))

    def range_statistics(self, request: m.RangeStatisticsRequest) -> m.RangeStatisticsResponse:
        span = request.span
        if isinstance(span, m.SampleSpan):
            found = find_silence_ranges(
                self.__connection, request.source_id, span.start_sample, span.end_sample)
            clips = [(row, min(row.end_sample, span.end_sample)
                      - max(row.start_sample, span.start_sample)) for row in found]
            unit, overlap_suffix = "sample", "_overlap_samples"
        else:
            found = find_quality_ranges(
                self.__connection, request.source_id, span.start_frame, span.end_frame)
            clips = [(row, min(row.end_frame, span.end_frame)
                      - max(row.start_frame, span.start_frame)) for row in found]
            unit, overlap_suffix = "frame", "_overlap_frames"
        totals: dict[tuple[str, str, str], int] = {}
        for row, overlap in clips:
            base = (row.artifact_sha, row.analyzer_version)
            for metric, delta in ((row.kind + overlap_suffix, overlap),
                                  (row.kind + "_range_count", 1)):
                totals[(*base, metric)] = totals.get((*base, metric), 0) + delta
        stats = tuple(
            m.RangeStatisticRow(
                source_id=request.source_id, span=span, metric=metric, value=value,
                unit="count" if metric.endswith("_range_count") else unit,
                analyzer_version=version, artifact_sha=artifact_sha)
            for (artifact_sha, version, metric), value in sorted(totals.items()))
        return m.RangeStatisticsResponse(total=len(stats), rows=stats)


__all__ = ["MediaQueryApi"]
