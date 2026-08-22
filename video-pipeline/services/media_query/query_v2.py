"""MediaQueryApiV2 — the typed, read-only v2 media-query surface (PRD 7.4).

Ten allowlisted methods over the canonical media-intelligence index built by
``index_v2.build_index``. v1 discipline inherited (frozen v1 files untouched):
typed requests; SQL composed only from frozen fragments with ``?``-bound data;
``V2_MAX_PAGE_SIZE``/``V2_ROW_BUDGET`` bounds; lineage on every row; no write
path. ``similar_shots`` never fabricates; semantic search uses the deterministic
keyword kernel from ``v2_rows`` (embeddings arrive later).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final, Self, TypeVar

import duckdb

from services.media_intelligence.models import Shot
from services.media_query import v2_models as vm
from services.media_query import v2_rows as rows
from services.media_query.index_v2 import open_read_only
from services.media_query.queries import _require_int as _int
from services.media_query.queries import _require_str as _str

_RowT = TypeVar("_RowT")
_RespT = TypeVar("_RespT")
_W_OVERLAP: Final = "start_frame < ? AND end_frame > ?"
_ORDER_SHOT: Final = " ORDER BY start_frame, end_frame, shot_id, artifact_sha"


def _apply_span(where: list[str], params: list[object], span: vm.FrameSpan | None,
    *, prefix: str = "") -> None:
    if span is not None:
        where.append(f"{prefix}{_W_OVERLAP}")
        params += [span.end_frame, span.start_frame]


class MediaQueryApiV2:
    """Read-only, allowlisted view over one v2 media-intelligence index."""

    __slots__ = ("__connection",)

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self.__connection = connection

    @classmethod
    def open(cls, index_path: Path) -> Self:
        return cls(open_read_only(index_path))

    def close(self) -> None:
        self.__connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __fetch(self, sql: str, params: list[object]) -> tuple[tuple[object, ...], ...]:
        return tuple(self.__connection.execute(sql, params).fetchall())

    def __scalar(self, sql: str, params: list[object]) -> int:
        row = self.__connection.execute(sql, params).fetchone()
        if row is None:
            raise TypeError("count query returned no rows")
        return _int(row[0])

    def __budgeted_total(self, what: str, sql: str, params: list[object]) -> int:
        total = self.__scalar(sql, params)
        if total > vm.V2_ROW_BUDGET:
            raise vm.ApiBudgetExceededV2(
                f"{what} matched {total} rows; v2 budget is {vm.V2_ROW_BUDGET}; "
                "narrow the span or filters")
        return total

    def __page_window(self, what: str, pagination: vm.V2Pagination) -> None:
        if pagination.offset + pagination.limit > vm.V2_ROW_BUDGET:
            raise vm.ApiBudgetExceededV2(
                f"{what} page window [{pagination.offset}, {pagination.offset + pagination.limit})"
                f" exceeds the v2 budget of {vm.V2_ROW_BUDGET} rows; narrow the query")

    def __paged(  # noqa: PLR0913 (fixed count/page plumbing, not caller data)
        self, what: str, pagination: vm.V2Pagination, *,
        count_sql: str, count_params: list[object], page_sql: str, page_params: list[object],
        build_row: Callable[[tuple[object, ...]], _RowT],
        build_response: Callable[[int, tuple[_RowT, ...]], _RespT],
    ) -> _RespT:
        self.__page_window(what, pagination)
        total = self.__budgeted_total(what, count_sql, count_params)
        fetched = self.__fetch(page_sql, page_params)
        return build_response(total, tuple(build_row(row) for row in fetched))

    def __echo(self, pagination: vm.V2Pagination) -> dict[str, int]:
        return {"limit": pagination.limit, "offset": pagination.offset}

    def shots(self, request: vm.ShotsRequest) -> vm.ShotsResponse:
        overlap = [request.span.end_frame, request.span.start_frame]
        return self.__paged(
            "shots", request.pagination,
            count_sql=f"SELECT count(*) FROM mi_shots WHERE {_W_OVERLAP}",  # noqa: S608
            count_params=[*overlap],
            page_sql="SELECT shot_id, artifact_sha, start_frame, end_frame, description, "  # noqa: S608
                     f"shot_size, camera_motion, framing, role, select_potential, pacing, "
                     f"confidence_editorial, confidence_visual FROM mi_shots WHERE "
                     f"{_W_OVERLAP}{_ORDER_SHOT} LIMIT ? OFFSET ?",
            page_params=[*overlap, request.pagination.limit, request.pagination.offset],
            build_row=rows.shot_row,
            build_response=lambda total, built: vm.ShotsResponse(
                total=total, rows=built, **self.__echo(request.pagination)))

    def shot_detail(self, request: vm.ShotDetailRequest) -> vm.ShotDetailResponse:
        row = self.__connection.execute(
            "SELECT payload, artifact_sha FROM mi_shots WHERE shot_id = ? "
            "ORDER BY artifact_sha LIMIT 1", [request.shot_id]).fetchone()
        if row is None:
            raise vm.ApiShotNotFoundV2(request.shot_id)
        return vm.ShotDetailResponse(
            total=1, shot=Shot.model_validate_json(_str(row[0])), artifact_sha=_str(row[1]))

    def best_moments(self, request: vm.BestMomentsRequest) -> vm.BestMomentsResponse:
        where, params = [], []
        _apply_span(where, params, request.span)
        if request.select_potentials is not None:
            placeholders = ",".join("?" * len(request.select_potentials))
            where.append(f"select_potential IN ({placeholders})")
            params += list(request.select_potentials)
        clause = " AND ".join(where) or "1=1"
        return self.__paged(
            "best_moments", request.pagination,
            count_sql=f"SELECT count(*) FROM mi_shots WHERE {clause}",  # noqa: S608
            count_params=[*params],
            page_sql="SELECT best_moment_frame, best_moment_why, shot_id, start_frame, "  # noqa: S608
                     f"end_frame, role, select_potential, artifact_sha FROM mi_shots "
                     f"WHERE {clause} ORDER BY best_moment_frame, shot_id, "
                     f"artifact_sha LIMIT ? OFFSET ?",
            page_params=[*params, request.pagination.limit, request.pagination.offset],
            build_row=rows.best_moment_row,
            build_response=lambda total, built: vm.BestMomentsResponse(
                total=total, rows=built, **self.__echo(request.pagination)))

    def transcript_range(self, request: vm.TranscriptRangeRequest) -> vm.TranscriptRangeResponse:
        window = [request.source_id, request.span.end_frame, request.span.start_frame]
        join = ("FROM mi_transcripts t JOIN mi_shots s ON s.shot_id = t.shot_id "
                "AND s.artifact_sha = t.artifact_sha WHERE s.source_id = ? "
                "AND t.start_frame < ? AND t.end_frame > ?")
        return self.__paged(
            "transcript_range", request.pagination,
            count_sql=f"SELECT count(*) {join}",
            count_params=[*window],
            page_sql="SELECT t.segment_id, t.start_frame, t.end_frame, t.text, t.shot_id, "
                     f"t.artifact_sha {join} "
                     "ORDER BY t.start_frame, t.shot_id, t.seq LIMIT ? OFFSET ?",
            page_params=[*window, request.pagination.limit, request.pagination.offset],
            build_row=rows.transcript_range_row,
            build_response=lambda total, built: vm.TranscriptRangeResponse(
                total=total, rows=built, **self.__echo(request.pagination)))

    def semantic_shot_search(self, request: vm.SemanticSearchRequest) -> vm.SemanticSearchResponse:
        self.__page_window("semantic_shot_search", request.pagination)
        where, params = [], []
        _apply_span(where, params, request.span)
        clause = " AND ".join(where) or "1=1"
        count_sql = f"SELECT count(*) FROM mi_shots WHERE {clause}"  # noqa: S608
        self.__budgeted_total("semantic_shot_search", count_sql, [*params])
        candidates = self.__fetch(
            "SELECT shot_id, artifact_sha, start_frame, end_frame, description FROM mi_shots "  # noqa: S608
            f"WHERE {clause}{_ORDER_SHOT}", [*params])
        text_rows = self.__fetch("SELECT shot_id, artifact_sha, field, text "
                                 "FROM mi_search_texts", [])
        keywords = request.text_query.split()
        hits = rows.semantic_ranked_hits(candidates, text_rows, keywords)
        start = request.pagination.offset
        page = hits[start:start + request.pagination.limit]
        return vm.SemanticSearchResponse(
            total=len(hits), rows=tuple(rows.semantic_hit_row(hit) for hit in page),
            **self.__echo(request.pagination))

    def similar_shots(self, request: vm.SimilarShotsRequest) -> vm.SimilarShotsResponse:
        known = self.__scalar("SELECT count(*) FROM mi_shots WHERE shot_id = ?", [request.shot_id])
        if known == 0:
            raise vm.ApiShotNotFoundV2(request.shot_id)
        total = self.__budgeted_total(
            "similar_shots", "SELECT count(*) FROM mi_similarity WHERE shot_id = ?",
            [request.shot_id])
        not_indexed = self.__scalar("SELECT count(*) FROM mi_similarity", []) == 0
        fetched = self.__fetch(
            "SELECT ref_shot_id, score, artifact_sha FROM mi_similarity WHERE shot_id = ? "
            "ORDER BY artifact_sha, seq, ref_shot_id", [request.shot_id])
        return vm.SimilarShotsResponse(
            not_indexed=not_indexed, total=total,
            rows=tuple(rows.similar_shot_row(row, request.shot_id) for row in fetched))

    def visible_text_candidates(
        self, request: vm.VisibleTextCandidatesRequest
    ) -> vm.VisibleTextCandidatesResponse:
        where, params = [], []
        _apply_span(where, params, request.span, prefix="s.")
        if request.text_query is not None:
            where.append("contains(lower(v.text), lower(?))")
            params.append(request.text_query)
        clause = " AND ".join(where) or "1=1"
        join = ("FROM mi_visible_texts v JOIN mi_shots s ON s.shot_id = v.shot_id "
                f"AND s.artifact_sha = v.artifact_sha WHERE {clause}")
        return self.__paged(
            "visible_text_candidates", request.pagination,
            count_sql=f"SELECT count(*) {join}",
            count_params=[*params],
            page_sql="SELECT v.shot_id, v.artifact_sha, s.start_frame, s.end_frame, v.text, "
                     f"v.frame {join} "
                     "ORDER BY s.start_frame, v.shot_id, v.seq LIMIT ? OFFSET ?",
            page_params=[*params, request.pagination.limit, request.pagination.offset],
            build_row=rows.visible_text_row,
            build_response=lambda total, built: vm.VisibleTextCandidatesResponse(
                total=total, rows=built, **self.__echo(request.pagination)))

    def visual_quality_ranges(
        self, request: vm.VisualQualityRangesRequest
    ) -> vm.VisualQualityRangesResponse:
        where, params = [], []
        _apply_span(where, params, request.span)
        if request.flags is not None:
            where.append("list_contains(?, flag)")
            params.append(list(request.flags))
        clause = " AND ".join(where) or "1=1"
        return self.__paged(
            "visual_quality_ranges", request.pagination,
            count_sql=f"SELECT count(*) FROM mi_quality WHERE {clause}",  # noqa: S608
            count_params=[*params],
            page_sql="SELECT shot_id, artifact_sha, start_frame, end_frame, flag, severity "  # noqa: S608
                     f"FROM mi_quality WHERE {clause} ORDER BY start_frame, shot_id, flag, "
                     f"artifact_sha LIMIT ? OFFSET ?",
            page_params=[*params, request.pagination.limit, request.pagination.offset],
            build_row=rows.quality_row,
            build_response=lambda total, built: vm.VisualQualityRangesResponse(
                total=total, rows=built, **self.__echo(request.pagination)))

    def audio_energy_ranges(
        self, request: vm.AudioEnergyRangesRequest
    ) -> vm.AudioEnergyRangesResponse:
        where, params = ["energy IS NOT NULL"], []
        if request.min_energy is not None:
            where.append("energy >= ?")
            params.append(request.min_energy)
        if request.max_energy is not None:
            where.append("energy <= ?")
            params.append(request.max_energy)
        _apply_span(where, params, request.span)
        clause = " AND ".join(where)
        return self.__paged(
            "audio_energy_ranges", request.pagination,
            count_sql=f"SELECT count(*) FROM mi_audio WHERE {clause}",  # noqa: S608
            count_params=[*params],
            page_sql="SELECT shot_id, artifact_sha, start_frame, end_frame, energy, "  # noqa: S608
                     f"loudness_db, ambient_type FROM mi_audio WHERE {clause} "
                     f"ORDER BY start_frame, shot_id, artifact_sha LIMIT ? OFFSET ?",
            page_params=[*params, request.pagination.limit, request.pagination.offset],
            build_row=rows.audio_energy_row,
            build_response=lambda total, built: vm.AudioEnergyRangesResponse(
                total=total, rows=built, **self.__echo(request.pagination)))

    def scene_summary(self, _request: vm.SceneSummaryRequest) -> vm.SceneSummaryResponse:
        episodes = self.__fetch(
            "SELECT episode_id, count(*), sum(end_frame - start_frame) FROM mi_shots "
            "GROUP BY episode_id ORDER BY episode_id", [])
        if len(episodes) > vm.V2_ROW_BUDGET:
            raise vm.ApiBudgetExceededV2(
                f"scene_summary matched {len(episodes)} episodes; v2 budget is "
                f"{vm.V2_ROW_BUDGET}")
        summary_rows = rows.scene_summary_rows(
            episodes,
            self.__fetch("SELECT episode_id, role, count(*) FROM mi_shots "
                         "GROUP BY episode_id, role ORDER BY episode_id, role", []),
            self.__fetch("SELECT episode_id, shot_size, count(*) FROM mi_shots "
                         "GROUP BY episode_id, shot_size ORDER BY episode_id, shot_size", []),
            self.__fetch("SELECT episode_id, count(*) FROM mi_sources GROUP BY episode_id", []),
            self.__fetch("SELECT episode_id, artifact_sha FROM mi_shots "
                         "GROUP BY episode_id, artifact_sha ORDER BY episode_id, artifact_sha",
                         []))
        return vm.SceneSummaryResponse(total=len(summary_rows), rows=summary_rows)


__all__ = ["MediaQueryApiV2"]
