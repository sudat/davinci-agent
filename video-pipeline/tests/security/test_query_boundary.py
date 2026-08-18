"""Security boundary of the read-only Media Query API (Todo 37, PRD 27).

The Editorial model receives a TYPED ALLOWLIST only: no review-example /
Review-Learning / past-Plan retrieval (Phase 5), no arbitrary SQL, no
write path, no path/network reach, no raw DB handle, and no unrestricted
frame dump.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest
from pydantic import ValidationError

from services.media_query.api_models import (
    FROZEN_METHOD_ALLOWLIST,
    FROZEN_PUBLIC_SURFACE,
    LIFECYCLE_SURFACE,
    CandidateFramesRequest,
    ContactSheetsRequest,
    EpisodeSummaryRequest,
    FrameSpan,
    Pagination,
    QualityRangesRequest,
    RangeStatisticsRequest,
    SampleSpan,
    SearchTranscriptsRequest,
    SilenceRangesRequest,
)
from services.media_query.index import MEDIA_DB_NAME
from tests.media_query.test_api import api
from tests.media_query.test_index import episode, lock

# module-level use registers the imported fixtures for this module
PYTEST_FIXTURES = (api, episode, lock)

if TYPE_CHECKING:
    from services.media_query.api import MediaQueryApi
    from tests.media_query.test_index import EpisodeEnv

API_MODULE = Path("services/media_query/api.py")
API_MODELS_MODULE = Path("services/media_query/api_models.py")

FORBIDDEN_METHOD_NAMES = (
    "get_review_examples",
    "get_review_learning",
    "query_review_examples",
    "find_review_examples",
    "get_past_plans",
    "query_past_plans",
    "get_selection_history",
    "get_plan_history",
)

REQUEST_MODELS = (
    EpisodeSummaryRequest,
    SearchTranscriptsRequest,
    SilenceRangesRequest,
    QualityRangesRequest,
    ContactSheetsRequest,
    CandidateFramesRequest,
    RangeStatisticsRequest,
)

WRITE_METHOD_NAMES = (
    "execute",
    "executemany",
    "insert",
    "update",
    "delete",
    "write",
    "upsert",
    "rebuild",
    "create",
    "drop",
    "commit",
    "rollback",
)

PATH_OR_NETWORK_IDS = (
    "../etc/passwd",
    "../../jobs/ep-001",
    "http://evil.example/x",
    "https://evil.example",
    "ftp://host/media",
    "/absolute/path",
    "src/../../../etc",
    "source id",
    "",
    ".",
)


def _all_requests() -> tuple[
    EpisodeSummaryRequest,
    SearchTranscriptsRequest,
    SilenceRangesRequest,
    QualityRangesRequest,
    ContactSheetsRequest,
    CandidateFramesRequest,
    RangeStatisticsRequest,
]:
    return (
        EpisodeSummaryRequest(source_id="edit-source-audio"),
        SearchTranscriptsRequest(
            source_id="edit-source-audio",
            text_query="今日",
            pagination=Pagination(limit=10, offset=0),
        ),
        SilenceRangesRequest(
            source_id="edit-source-audio",
            span=SampleSpan(start_sample=0, end_sample=480000),
            pagination=Pagination(limit=10, offset=0),
        ),
        QualityRangesRequest(
            source_id="edit-source-video",
            span=FrameSpan(start_frame=0, end_frame=80),
            pagination=Pagination(limit=10, offset=0),
        ),
        ContactSheetsRequest(source_id="edit-source-video"),
        CandidateFramesRequest(
            source_id="edit-source-video",
            span=FrameSpan(start_frame=0, end_frame=80),
        ),
        RangeStatisticsRequest(
            source_id="edit-source-audio",
            span=SampleSpan(start_sample=0, end_sample=480000),
        ),
    )


def test_api_surface_is_frozen_allowlist(api: MediaQueryApi) -> None:
    public = {name for name in dir(api) if not name.startswith("_")}
    assert public == FROZEN_PUBLIC_SURFACE  # dir() surface is exactly frozen
    assert public - LIFECYCLE_SURFACE == FROZEN_METHOD_ALLOWLIST
    assert frozenset(
        {
            "episode_summary",
            "search_transcripts",
            "silence_ranges",
            "quality_ranges",
            "contact_sheets",
            "candidate_frames",
            "range_statistics",
        }
    ) == FROZEN_METHOD_ALLOWLIST
    # absence by name AND by behavior: review/past-plan retrieval is absent
    for name in FORBIDDEN_METHOD_NAMES:
        assert not hasattr(api, name)
        with pytest.raises(AttributeError):
            getattr(api, name)
    # grep/AST guard: neither module defines any forbidden retrieval
    for module_path in (API_MODULE, API_MODELS_MODULE):
        source = module_path.read_text()
        for name in FORBIDDEN_METHOD_NAMES:
            assert name not in source, f"{name} must not appear in {module_path}"
        tree = ast.parse(source)
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        }
        assert not defined & set(FORBIDDEN_METHOD_NAMES)
        for name in defined:
            assert "review" not in name.lower(), f"{name} in {module_path}"
            assert "past_plan" not in name.lower(), f"{name} in {module_path}"
            assert "history" not in name.lower(), f"{name} in {module_path}"


def test_sql_and_sql_like_text_is_literal(episode: EpisodeEnv, api: MediaQueryApi) -> None:
    # no request model exposes sql/shell/path-like fields
    for model in REQUEST_MODELS:
        fields = set(model.model_fields)
        assert not fields & {
            "sql",
            "query",
            "statement",
            "command",
            "path",
            "url",
            "endpoint",
            "shell",
            "argv",
        }, model.__name__
        assert "source_id" in fields, model.__name__
    # SQL-looking text_query values are searched as literal text
    for sqlish in (
        "'; DROP TABLE transcript_segments; --",
        "SELECT * FROM sources",
        "DELETE FROM statistics WHERE 1=1",
    ):
        result = api.search_transcripts(
            SearchTranscriptsRequest(
                source_id="edit-source-audio",
                text_query=sqlish,
                pagination=Pagination(limit=10, offset=0),
            )
        )
        assert result.total == 0
        assert result.rows == ()
    # nothing executed: the tables are intact and queries still work
    connection = duckdb.connect(str(episode.episode_dir / MEDIA_DB_NAME), read_only=True)
    try:
        tables = {str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()}
        assert {
            "sources",
            "transcript_segments",
            "statistics",
            "quality_ranges",
        } <= tables
        row = connection.execute(
            "SELECT count(*) FROM transcript_segments").fetchone()
        assert row is not None
        count = int(row[0])
        assert count == 3
    finally:
        connection.close()
    still = api.search_transcripts(
        SearchTranscriptsRequest(
            source_id="edit-source-audio",
            text_query="今日",
            pagination=Pagination(limit=10, offset=0),
        )
    )
    assert still.total == 1


def test_no_write_path_exists(api: MediaQueryApi) -> None:
    for name in WRITE_METHOD_NAMES:
        with pytest.raises(AttributeError):
            getattr(api, name)
    # __slots__ freezes the instance: no attribute injection, no __dict__
    assert not hasattr(api, "__dict__")
    with pytest.raises(AttributeError):
        setattr(api, "execute", print)  # noqa: B010 (runtime probe of a frozen surface)
    with pytest.raises(AttributeError):
        setattr(api, "get_review_examples", print)  # noqa: B010 (runtime probe)
    # the private connection is name-mangled out of any accidental reach
    with pytest.raises(AttributeError):
        getattr(api, "_connection")  # noqa: B009 (runtime probe of a private name)
    with pytest.raises(AttributeError):
        getattr(api, "connection")  # noqa: B009 (runtime probe of an absent name)


def test_path_and_network_source_ids_refused(api: MediaQueryApi) -> None:
    for bad in PATH_OR_NETWORK_IDS:
        with pytest.raises(ValidationError):
            EpisodeSummaryRequest(source_id=bad)
        with pytest.raises(ValidationError):
            SearchTranscriptsRequest(
                source_id=bad,
                text_query="x",
                pagination=Pagination(limit=10, offset=0),
            )
    # the strict identifier pattern still admits the real source ids
    assert api.episode_summary(EpisodeSummaryRequest(source_id="edit-source-audio")).total == 2


def _assert_no_leaks(value: object) -> None:
    assert not isinstance(value, duckdb.DuckDBPyConnection)
    assert not isinstance(value, bytes | bytearray)
    if isinstance(value, dict):
        for child in value.values():
            _assert_no_leaks(child)
    elif isinstance(value, list | tuple):
        for child in value:
            _assert_no_leaks(child)


def test_raw_database_never_leaks(api: MediaQueryApi) -> None:
    for name in dir(api):
        if name.startswith("_"):
            continue
        _assert_no_leaks(getattr(api, name))
    (
        summary,
        search,
        silence,
        quality,
        sheets,
        frames,
        stats,
    ) = _all_requests()
    responses = [
        api.episode_summary(summary),
        api.search_transcripts(search),
        api.silence_ranges(silence),
        api.quality_ranges(quality),
        api.contact_sheets(sheets),
        api.candidate_frames(frames),
        api.range_statistics(stats),
    ]
    assert len(responses) == 7
    for response in responses:
        assert response.total > 0
        _assert_no_leaks(response.model_dump())


def test_candidate_frames_response_size_bound(api: MediaQueryApi) -> None:
    response = api.candidate_frames(
        CandidateFramesRequest(
            source_id="edit-source-video",
            span=FrameSpan(start_frame=0, end_frame=80),
        )
    )
    assert response.total == 8
    expected_fields = {
        "source_id",
        "span",
        "confidence",
        "analyzer_version",
        "artifact_sha",
        "frame_index",
        "sheet_sha256",
    }
    for row in response.rows:
        assert set(type(row).model_fields) == expected_fields
        assert isinstance(row.frame_index, int)
        assert len(row.sheet_sha256) == 64
        assert len(row.model_dump_json()) < 400
    assert len(response.model_dump_json()) < 8000
