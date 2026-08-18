"""MediaQueryApi: bounded, lineage-complete, read-only queries (Todo 37).

Builds a real tmp index through the Todo-36 fixture pattern (the session
``episode`` fixture: real Todo 33-35 analyzer artifacts over tiny
synthesized media), then proves every allowlisted method returns bounded
lineage-complete results, LIMIT/OFFSET pagination works, the frozen row
budget refuses oversized requests, and no method ever mutates the
read-only index.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb
import pytest
from pydantic import ValidationError

from services.media_query.api import MediaQueryApi
from services.media_query.api_models import (
    FROZEN_MAX_PAGE_SIZE,
    FROZEN_ROW_BUDGET,
    ApiBudgetExceeded,
    CandidateFramesRequest,
    ContactSheetsRequest,
    EpisodeSummaryRequest,
    FrameSpan,
    MsSpan,
    Pagination,
    QualityRangesRequest,
    RangeStatisticsRequest,
    SampleSpan,
    SearchTranscriptsRequest,
    SilenceRangesRequest,
)
from services.media_query.index import MEDIA_DB_NAME, MediaQueryError, MediaQueryIndex
from tests.media_query.test_index import (
    TRANSCRIPT_SEGMENTS,
    _build_transcript,
    episode,
    lock,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.media_query.test_index import EpisodeEnv

# module-level use registers the imported Todo-36 fixtures for this module
PYTEST_FIXTURES = (episode, lock)


@pytest.fixture
def api(episode: EpisodeEnv) -> Iterator[MediaQueryApi]:
    built = MediaQueryIndex.open(episode.episode_dir / MEDIA_DB_NAME)
    try:
        built.rebuild(episode.store, episode.registry, episode.episode_dir)
    finally:
        built.close()
    with MediaQueryApi.open(episode.episode_dir / MEDIA_DB_NAME) as opened:
        yield opened


def _bulk_transcript(count: int, marker: str):
    segments = tuple(
        (index * 10, index * 10 + 5, f"{marker}{index}") for index in range(count)
    )
    return _build_transcript(segments)


def _table_counts(path) -> dict[str, int]:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        tables = [
            str(row[0]) for row in connection.execute("SHOW TABLES").fetchall()
        ]
        counts = {}
        # table names come from this fixture DB's own SHOW TABLES, never callers
        for table in sorted(tables):
            row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
            assert row is not None
            counts[table] = int(row[0])
        return counts
    finally:
        connection.close()


# ---------------------------------------------------------------- happy


def test_episode_summary_rows_carry_lineage_and_no_paths(
    episode: EpisodeEnv, api: MediaQueryApi
) -> None:
    audio = api.episode_summary(EpisodeSummaryRequest(source_id="edit-source-audio"))
    assert audio.total == 2
    assert {row.artifact_sha for row in audio.rows} == {
        episode.receipts[0].content_sha256,
        episode.receipts[1].content_sha256,
    }
    rates_by_sha = {row.artifact_sha: row.sample_rate for row in audio.rows}
    assert rates_by_sha == {
        episode.receipts[0].content_sha256: 16000,
        episode.receipts[1].content_sha256: 48000,
    }
    for row in audio.rows:
        assert row.source_id == "edit-source-audio"
        assert row.span is None
        assert row.confidence is None
        assert row.source_kind == "audio"
        assert row.sha256 != ""
    video = api.episode_summary(EpisodeSummaryRequest(source_id="edit-source-video"))
    assert video.total == 1
    assert video.rows[0].artifact_sha == episode.receipts[2].content_sha256
    assert video.rows[0].source_kind == "video"
    assert video.rows[0].frame_count == 80
    assert video.rows[0].width == 320
    assert video.rows[0].analyzer_version == "todo35-v1"
    # no filesystem path leaks into the summary contract
    assert "path" not in type(audio.rows[0]).model_fields
    empty = api.episode_summary(EpisodeSummaryRequest(source_id="unknown-source"))
    assert empty.total == 0
    assert empty.rows == ()


def test_search_transcripts_exact_substring_and_pagination(episode: EpisodeEnv) -> None:
    bulk = _bulk_transcript(12, "ページ")
    built = MediaQueryIndex.open(episode.episode_dir / MEDIA_DB_NAME)
    try:
        built.rebuild(episode.store, episode.registry, episode.episode_dir)
        built.upsert_analyzer_artifact(bulk)
    finally:
        built.close()
    with MediaQueryApi.open(episode.episode_dir / MEDIA_DB_NAME) as api:
        hit = api.search_transcripts(
            SearchTranscriptsRequest(
                source_id="edit-source-audio",
                text_query="今日",
                pagination=Pagination(limit=FROZEN_MAX_PAGE_SIZE, offset=0),
            )
        )
        assert hit.total == 1
        row = hit.rows[0]
        assert row.text == TRANSCRIPT_SEGMENTS[0][2]
        assert row.span == MsSpan(start_ms=0, end_ms=5000)
        assert row.analyzer_version == "todo33-v1"
        assert row.artifact_sha == episode.receipts[0].content_sha256

        paged = api.search_transcripts(
            SearchTranscriptsRequest(
                source_id="edit-source-audio",
                text_query="ページ",
                pagination=Pagination(limit=5, offset=5),
            )
        )
        assert paged.total == 12
        assert paged.limit == 5
        assert paged.offset == 5
        assert [row.span.start_ms for row in paged.rows] == [50, 60, 70, 80, 90]
        first_page = api.search_transcripts(
            SearchTranscriptsRequest(
                source_id="edit-source-audio",
                text_query="ページ",
                pagination=Pagination(limit=5, offset=0),
            )
        )
        assert len(first_page.rows) == 5
        miss = api.search_transcripts(
            SearchTranscriptsRequest(
                source_id="edit-source-audio",
                text_query="ページ%d",
                pagination=Pagination(limit=5, offset=0),
            )
        )
        assert miss.total == 0
        assert miss.rows == ()


def test_silence_ranges_span_and_pagination(api: MediaQueryApi) -> None:
    request = SilenceRangesRequest(
        source_id="edit-source-audio",
        span=SampleSpan(start_sample=240000, end_sample=264000),
        pagination=Pagination(limit=FROZEN_MAX_PAGE_SIZE, offset=0),
    )
    found = api.silence_ranges(request)
    assert found.total == 2
    assert [row.kind for row in found.rows] == ["measured_silence", "pause"]
    for row in found.rows:
        assert row.span == SampleSpan(start_sample=240000, end_sample=264000)
        assert row.sample_rate == 48000
        assert row.analyzer_version == "todo34-v1"
    pause = found.rows[1]
    assert pause.confidence == 700
    measured = found.rows[0]
    assert measured.confidence is None
    # offset pages: second page holds only the pause candidate
    page_two = api.silence_ranges(
        SilenceRangesRequest(
            source_id="edit-source-audio",
            span=SampleSpan(start_sample=240000, end_sample=264000),
            pagination=Pagination(limit=1, offset=1),
        )
    )
    assert page_two.total == 2
    assert [row.kind for row in page_two.rows] == ["pause"]
    # half-open span semantics: adjacent-but-disjoint sample ranges miss
    disjoint = api.silence_ranges(
        SilenceRangesRequest(
            source_id="edit-source-audio",
            span=SampleSpan(start_sample=264000, end_sample=300000),
            pagination=Pagination(limit=10, offset=0),
        )
    )
    assert disjoint.total == 0
    assert disjoint.rows == ()


def test_quality_ranges_span_and_pagination(api: MediaQueryApi) -> None:
    found = api.quality_ranges(
        QualityRangesRequest(
            source_id="edit-source-video",
            span=FrameSpan(start_frame=0, end_frame=80),
            pagination=Pagination(limit=2, offset=0),
        )
    )
    assert found.total == 3
    assert [(row.kind, row.span.start_frame, row.span.end_frame) for row in found.rows] == [
        ("black", 0, 10),
        ("blur", 30, 50),
    ]
    assert found.rows[0].rule_id == "black-mean-fraction-v1"
    assert found.rows[0].confidence == 900
    page_two = api.quality_ranges(
        QualityRangesRequest(
            source_id="edit-source-video",
            span=FrameSpan(start_frame=0, end_frame=80),
            pagination=Pagination(limit=2, offset=2),
        )
    )
    assert [(row.kind, row.span.start_frame, row.span.end_frame) for row in page_two.rows] == [
        ("exposure_over", 50, 60)
    ]


def test_contact_sheets_bounded_refs(episode: EpisodeEnv, api: MediaQueryApi) -> None:
    found = api.contact_sheets(ContactSheetsRequest(source_id="edit-source-video"))
    assert found.total == 1
    sheet = found.rows[0]
    assert sheet.frame_indexes == (0, 10, 20, 30, 40, 50, 60, 70)
    assert (sheet.cols, sheet.rows, sheet.thumb_w, sheet.thumb_h) == (5, 2, 64, 36)
    assert sheet.cadence_frames == 10
    assert sheet.span == FrameSpan(start_frame=0, end_frame=71)
    assert sheet.artifact_sha == episode.receipts[2].content_sha256
    assert sheet.analyzer_version == "todo35-v1"
    # refs only: no filesystem path field in the contract
    assert "path" not in type(sheet).model_fields
    assert "sheet_path" not in type(sheet).model_fields


def test_candidate_frames_indexes_and_refs_only(
    episode: EpisodeEnv, api: MediaQueryApi
) -> None:
    found = api.candidate_frames(
        CandidateFramesRequest(
            source_id="edit-source-video",
            span=FrameSpan(start_frame=0, end_frame=35),
        )
    )
    assert found.total == 4
    assert [row.frame_index for row in found.rows] == [0, 10, 20, 30]
    for row in found.rows:
        assert row.span == FrameSpan(start_frame=row.frame_index, end_frame=row.frame_index + 1)
        assert row.sheet_sha256 == found.rows[0].sheet_sha256
        assert row.artifact_sha == episode.receipts[2].content_sha256
        assert row.confidence is None


def test_range_statistics_deterministic_per_artifact(
    episode: EpisodeEnv, api: MediaQueryApi
) -> None:
    audio = api.range_statistics(
        RangeStatisticsRequest(
            source_id="edit-source-audio",
            span=SampleSpan(start_sample=250000, end_sample=260000),
        )
    )
    assert audio.total == len(audio.rows) == 4
    metrics = {row.metric: row.value for row in audio.rows}
    assert metrics == {
        "measured_silence_overlap_samples": 10000,
        "measured_silence_range_count": 1,
        "pause_overlap_samples": 10000,
        "pause_range_count": 1,
    }
    for row in audio.rows:
        assert row.span == SampleSpan(start_sample=250000, end_sample=260000)
        assert row.unit in {"sample", "count"}
        assert row.artifact_sha == episode.receipts[1].content_sha256
        assert row.analyzer_version == "todo34-v1"
        assert row.confidence is None
    video = api.range_statistics(
        RangeStatisticsRequest(
            source_id="edit-source-video",
            span=FrameSpan(start_frame=0, end_frame=80),
        )
    )
    video_metrics = {(row.metric) for row in video.rows}
    assert video_metrics == {
        "black_overlap_frames",
        "black_range_count",
        "blur_overlap_frames",
        "blur_range_count",
        "exposure_over_overlap_frames",
        "exposure_over_range_count",
    }
    values = {row.metric: row.value for row in video.rows}
    assert values["black_overlap_frames"] == 10
    assert values["blur_overlap_frames"] == 20
    assert values["exposure_over_range_count"] == 1
    empty = api.range_statistics(
        RangeStatisticsRequest(
            source_id="edit-source-audio",
            span=SampleSpan(start_sample=264000, end_sample=300000),
        )
    )
    assert empty.total == 0
    assert empty.rows == ()


def test_response_models_carry_no_floats(api: MediaQueryApi) -> None:
    responses = [
        api.episode_summary(EpisodeSummaryRequest(source_id="edit-source-audio")),
        api.search_transcripts(
            SearchTranscriptsRequest(
                source_id="edit-source-audio",
                text_query="今日",
                pagination=Pagination(limit=10, offset=0),
            )
        ),
        api.silence_ranges(
            SilenceRangesRequest(
                source_id="edit-source-audio",
                span=SampleSpan(start_sample=0, end_sample=480000),
                pagination=Pagination(limit=10, offset=0),
            )
        ),
        api.quality_ranges(
            QualityRangesRequest(
                source_id="edit-source-video",
                span=FrameSpan(start_frame=0, end_frame=80),
                pagination=Pagination(limit=10, offset=0),
            )
        ),
        api.contact_sheets(ContactSheetsRequest(source_id="edit-source-video")),
        api.candidate_frames(
            CandidateFramesRequest(
                source_id="edit-source-video",
                span=FrameSpan(start_frame=0, end_frame=80),
            )
        ),
        api.range_statistics(
            RangeStatisticsRequest(
                source_id="edit-source-video",
                span=FrameSpan(start_frame=0, end_frame=80),
            )
        ),
    ]

    def walk(value: object) -> None:
        assert not isinstance(value, float)
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list | tuple):
            for child in value:
                walk(child)

    for response in responses:
        walk(response.model_dump())


# -------------------------------------------------------------- failure


def test_row_budget_exceeded_refusal(episode: EpisodeEnv) -> None:
    bulk = _bulk_transcript(600, "バルク文")
    built = MediaQueryIndex.open(episode.episode_dir / MEDIA_DB_NAME)
    try:
        built.rebuild(episode.store, episode.registry, episode.episode_dir)
        built.upsert_analyzer_artifact(bulk)
    finally:
        built.close()
    with MediaQueryApi.open(episode.episode_dir / MEDIA_DB_NAME) as api:
        oversized = SearchTranscriptsRequest(
            source_id="edit-source-audio",
            text_query="バルク文",
            pagination=Pagination(limit=FROZEN_MAX_PAGE_SIZE, offset=0),
        )
        with pytest.raises(ApiBudgetExceeded, match="budget-exceeded") as caught:
            api.search_transcripts(oversized)
        assert caught.value.code == "budget-exceeded"
        # window beyond the frozen budget is refused even for tiny totals
        window = SearchTranscriptsRequest(
            source_id="edit-source-audio",
            text_query="今日",
            pagination=Pagination(limit=2, offset=FROZEN_ROW_BUDGET - 1),
        )
        with pytest.raises(ApiBudgetExceeded, match="budget-exceeded"):
            api.search_transcripts(window)


def test_malformed_requests_refused(api: MediaQueryApi) -> None:
    with pytest.raises(ValidationError):
        Pagination(limit=0, offset=0)
    with pytest.raises(ValidationError):
        Pagination(limit=FROZEN_MAX_PAGE_SIZE + 1, offset=0)
    with pytest.raises(ValidationError):
        Pagination(limit=10, offset=-1)
    with pytest.raises(ValidationError):
        SampleSpan(start_sample=100, end_sample=50)
    with pytest.raises(ValidationError):
        FrameSpan(start_frame=10, end_frame=5)
    with pytest.raises(ValidationError):
        MsSpan(start_ms=10, end_ms=5)
    with pytest.raises(ValidationError):
        SearchTranscriptsRequest(
            source_id="edit-source-audio",
            text_query="",
            pagination=Pagination(limit=10, offset=0),
        )
    with pytest.raises(ValidationError):
        SearchTranscriptsRequest(
            source_id="edit-source-audio",
            text_query="x" * 201,
            pagination=Pagination(limit=10, offset=0),
        )
    # strict typing: floats are refused everywhere, ints only
    with pytest.raises(ValidationError):
        SampleSpan(start_sample=1.5, end_sample=2)  # type: ignore[arg-type]


def test_api_is_read_only_and_never_mutates(episode: EpisodeEnv, api: MediaQueryApi) -> None:
    db_path = episode.episode_dir / MEDIA_DB_NAME
    before = _table_counts(db_path)
    api.episode_summary(EpisodeSummaryRequest(source_id="edit-source-audio"))
    api.search_transcripts(
        SearchTranscriptsRequest(
            source_id="edit-source-audio",
            text_query="今日",
            pagination=Pagination(limit=10, offset=0),
        )
    )
    api.silence_ranges(
        SilenceRangesRequest(
            source_id="edit-source-audio",
            span=SampleSpan(start_sample=0, end_sample=480000),
            pagination=Pagination(limit=10, offset=0),
        )
    )
    api.quality_ranges(
        QualityRangesRequest(
            source_id="edit-source-video",
            span=FrameSpan(start_frame=0, end_frame=80),
            pagination=Pagination(limit=10, offset=0),
        )
    )
    api.contact_sheets(ContactSheetsRequest(source_id="edit-source-video"))
    api.candidate_frames(
        CandidateFramesRequest(
            source_id="edit-source-video",
            span=FrameSpan(start_frame=0, end_frame=80),
        )
    )
    api.range_statistics(
        RangeStatisticsRequest(
            source_id="edit-source-audio",
            span=SampleSpan(start_sample=0, end_sample=480000),
        )
    )
    assert _table_counts(db_path) == before


def test_missing_or_stale_index_refused_structured(tmp_path) -> None:
    with pytest.raises(MediaQueryError, match="index-unreadable"):
        MediaQueryApi.open(tmp_path / "missing.duckdb")
    stale = tmp_path / "stale.duckdb"
    stale.write_bytes(b"not a duckdb file")
    with pytest.raises(MediaQueryError, match="index-unreadable"):
        MediaQueryApi.open(stale)
