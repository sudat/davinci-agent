"""MediaQueryApiV2: typed, read-only, budgeted queries over canonical shots.

Builds a DuckDB v2 index from a synthetic ``MediaIntelligenceArtifact``
(Task-12 model), then proves: every one of the ten PRD 7.4 methods returns
bounded lineage-complete rows; the v2 budget/page caps are enforced; the
index bytes and mtime are untouched by queries (read-only contract);
``similar_shots`` never fabricates when no similarity index exists; and
unknown shot ids raise the typed NotFound error.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.media_intelligence.models import (
    AudioMeasurements,
    EditSourceSpan,
    LocationInfo,
    MediaIntelligenceArtifact,
    MediaSource,
    Shot,
    ShotBestMoment,
    ShotConfidence,
    ShotCuttability,
    ShotEditorial,
    ShotVisual,
    SimilarityRef,
    TranscriptSegment,
    VisibleText,
)
from services.media_query import v2_models as vm
from services.media_query.index_v2 import artifact_content_sha, build_index
from services.media_query.query_v2 import MediaQueryApiV2

EPISODE_ID = "ep-001"
SOURCE_ID = "src-cam-a"


def _shot(  # noqa: PLR0913 (synthetic fixture table: one kwarg per indexed evidence kind)
    shot_id: str,
    start: int,
    end: int,
    *,
    description: str,
    role: str,
    potential: str,
    shot_size: str = "medium",
    motion: str = "static",
    flags: tuple[str, ...] = (),
    transcripts: tuple[tuple[str, str, int, int], ...] = (),
    visible: tuple[tuple[str, int], ...] = (),
    audio: AudioMeasurements | None = None,
    similarity: tuple[tuple[str, float], ...] = (),
    location: LocationInfo | None = None,
    best_frame: int | None = None,
    why: str = "決定瞬間",
) -> Shot:
    best = best_frame if best_frame is not None else (start + end) // 2
    return Shot(
        shot_id=shot_id,
        source_span=EditSourceSpan(start_frame=start, end_frame=end),
        description=description,
        visual=ShotVisual(shot_size=shot_size, camera_motion=motion, quality_flags=flags),
        editorial=ShotEditorial(
            role=role,
            select_potential=potential,
            best_moment=ShotBestMoment(frame=best, why=why),
            pacing="moderate",
            cuttability=ShotCuttability.model_validate({"in": "clean", "out": "clean"}),
        ),
        transcript_segments=tuple(
            TranscriptSegment(segment_id=seg_id, text=text, start_frame=s, end_frame=e)
            for seg_id, text, s, e in transcripts
        )
        or None,
        visible_texts=tuple(VisibleText(text=text, frame=frame) for text, frame in visible)
        or None,
        audio_measurements=audio,
        similarity_refs=tuple(
            SimilarityRef(ref_shot_id=ref, score=score) for ref, score in similarity
        )
        or None,
        location=location,
        confidence=ShotConfidence(editorial="medium", visual="high"),
    )


SHOT_A = _shot(
    "shot-a",
    0,
    100,
    description="商品を手に取って軽量な製品を説明する product close-up demo",
    role="b_roll",
    potential="medium",
    shot_size="close_up",
    flags=("blur",),
    transcripts=(("tr-a1", "この製品は軽量です", 10, 90),),
    visible=(("SPEC SHEET", 20),),
    audio=AudioMeasurements(loudness_db=-18.5, energy=0.4, ambient_type="room"),
    similarity=(("shot-x1", 0.9),),
    location=LocationInfo(location="studio", description="白い背景のスタジオ"),
    best_frame=50,
    why="製品が正面を向く",
)
SHOT_B = _shot(
    "shot-b",
    100,
    200,
    description="two people talking about editing workflow in the studio",
    role="talking_head",
    potential="high",
    motion="handheld",
    transcripts=(("tr-b1", "今日は編集の話をします", 110, 190),),
    visible=(("INTERVIEW", 120),),
    audio=AudioMeasurements(loudness_db=-12.0, energy=0.8, ambient_type="speech"),
    best_frame=150,
    why="両者が笑う",
)
SHOT_C = _shot(
    "shot-c",
    200,
    300,
    description="wide establishing shot of the city skyline at dusk",
    role="b_roll",
    potential="low",
    shot_size="wide",
    flags=("black", "noise"),
    best_frame=250,
    why="街灯が点く",
)

ARTIFACT = MediaIntelligenceArtifact(
    episode_id=EPISODE_ID,
    sources=(MediaSource(source_id=SOURCE_ID, duration_frames=300),),
    shots=(SHOT_A, SHOT_B, SHOT_C),
)

NO_SIMILARITY_ARTIFACT = MediaIntelligenceArtifact(
    episode_id=EPISODE_ID,
    sources=(MediaSource(source_id=SOURCE_ID, duration_frames=20),),
    shots=(_shot("shot-only", 0, 20, description="single talking shot", role="talking_head",
                 potential="high"),),
)


def _bulk_artifact(count: int) -> MediaIntelligenceArtifact:
    shots = tuple(
        _shot(
            f"shot-bulk-{index:04d}",
            index * 10,
            index * 10 + 10,
            description=f"bulk filler shot number {index}",
            role="coverage",
            potential="low",
        )
        for index in range(count)
    )
    return MediaIntelligenceArtifact(
        episode_id="ep-bulk",
        sources=(MediaSource(source_id="src-bulk", duration_frames=count * 10),),
        shots=shots,
    )


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return build_index(ARTIFACT, tmp_path / "media-intelligence-v2.duckdb")


@pytest.fixture
def api(index_path: Path) -> Iterator[MediaQueryApiV2]:
    with MediaQueryApiV2.open(index_path) as opened:
        yield opened


def _page() -> vm.V2Pagination:
    return vm.V2Pagination(limit=50, offset=0)


# ---------------------------------------------------------------- surface


def test_public_surface_is_exactly_the_ten_v2_methods(api: MediaQueryApiV2) -> None:
    public = {
        name for name in dir(api) if not name.startswith("_") and callable(getattr(api, name))
    }
    assert public == vm.V2_PUBLIC_SURFACE
    assert len(vm.V2_METHOD_ALLOWLIST) == 10


# ---------------------------------------------------------------- happy paths


def test_shots_in_range_uses_half_open_overlap(index_path: Path) -> None:
    with MediaQueryApiV2.open(index_path) as api:
        response = api.shots(
            vm.ShotsRequest(span=vm.FrameSpan(start_frame=150, end_frame=250), pagination=_page())
        )
    assert response.total == 2
    assert [row.shot_id for row in response.rows] == ["shot-b", "shot-c"]
    expected_sha = artifact_content_sha(ARTIFACT)
    for row in response.rows:
        assert row.artifact_sha == expected_sha
        assert row.span.end_frame > 150
        assert row.span.start_frame < 250
    assert response.rows[0].role == "talking_head"
    assert response.rows[0].select_potential == "high"
    assert response.rows[1].shot_size == "wide"


def test_shot_detail_returns_canonical_shot_losslessly(api: MediaQueryApiV2) -> None:
    response = api.shot_detail(vm.ShotDetailRequest(shot_id="shot-a"))
    assert response.shot == SHOT_A
    assert response.artifact_sha == artifact_content_sha(ARTIFACT)
    assert response.shot.editorial.best_moment.why == "製品が正面を向く"


def test_best_moments_filters_by_potential_and_span(api: MediaQueryApiV2) -> None:
    high_only = api.best_moments(
        vm.BestMomentsRequest(select_potentials=("high",), pagination=_page())
    )
    assert [row.shot_id for row in high_only.rows] == ["shot-b"]
    assert high_only.rows[0].frame == 150
    assert high_only.rows[0].why == "両者が笑う"

    windowed = api.best_moments(
        vm.BestMomentsRequest(span=vm.FrameSpan(start_frame=0, end_frame=150), pagination=_page())
    )
    assert [row.shot_id for row in windowed.rows] == ["shot-a", "shot-b"]


def test_transcript_range_by_source_and_span(api: MediaQueryApiV2) -> None:
    response = api.transcript_range(
        vm.TranscriptRangeRequest(
            source_id=SOURCE_ID,
            span=vm.FrameSpan(start_frame=0, end_frame=150),
            pagination=_page(),
        )
    )
    assert response.total == 2
    assert [row.segment_id for row in response.rows] == ["tr-a1", "tr-b1"]
    assert response.rows[0].text == "この製品は軽量です"
    assert response.rows[0].span == vm.FrameSpan(start_frame=10, end_frame=90)
    assert response.rows[0].artifact_sha == artifact_content_sha(ARTIFACT)

    unknown = api.transcript_range(
        vm.TranscriptRangeRequest(
            source_id="src-unknown",
            span=vm.FrameSpan(start_frame=0, end_frame=150),
            pagination=_page(),
        )
    )
    assert unknown.total == 0
    assert unknown.rows == ()


def test_semantic_search_hits_ja_and_en_keywords(api: MediaQueryApiV2) -> None:
    ja = api.semantic_shot_search(
        vm.SemanticSearchRequest(text_query="軽量", pagination=_page())
    )
    assert [row.shot_id for row in ja.rows] == ["shot-a"]
    assert row_score(ja.rows[0]) >= 2
    assert "description" in ja.rows[0].matched_fields
    assert "transcript" in ja.rows[0].matched_fields

    en = api.semantic_shot_search(
        vm.SemanticSearchRequest(text_query="PRODUCT", pagination=_page())
    )
    assert [row.shot_id for row in en.rows] == ["shot-a"]
    assert "description" in en.rows[0].matched_fields

    both = api.semantic_shot_search(
        vm.SemanticSearchRequest(text_query="編集 studio", pagination=_page())
    )
    assert [row.shot_id for row in both.rows] == ["shot-b", "shot-a"]
    assert row_score(both.rows[0]) >= 2
    assert row_score(both.rows[1]) == 1
    assert "location" in both.rows[1].matched_fields

    none_hit = api.semantic_shot_search(
        vm.SemanticSearchRequest(text_query="存在しない語彙 xyzzy", pagination=_page())
    )
    assert none_hit.total == 0
    assert none_hit.rows == ()


def row_score(row: vm.SemanticHitRow) -> int:
    return row.score


def test_similar_shots_returns_rows_and_indexed_flag(api: MediaQueryApiV2) -> None:
    hit = api.similar_shots(vm.SimilarShotsRequest(shot_id="shot-a"))
    assert hit.not_indexed is False
    assert hit.total == 1
    assert hit.rows[0].ref_shot_id == "shot-x1"
    assert hit.rows[0].score == pytest.approx(0.9)
    assert hit.rows[0].artifact_sha == artifact_content_sha(ARTIFACT)

    unlinked = api.similar_shots(vm.SimilarShotsRequest(shot_id="shot-b"))
    assert unlinked.not_indexed is False
    assert unlinked.total == 0
    assert unlinked.rows == ()


def test_similar_shots_absent_index_reports_not_indexed(tmp_path: Path) -> None:
    path = build_index(NO_SIMILARITY_ARTIFACT, tmp_path / "no-sim.duckdb")
    with MediaQueryApiV2.open(path) as api:
        response = api.similar_shots(vm.SimilarShotsRequest(shot_id="shot-only"))
    assert response.not_indexed is True
    assert response.total == 0
    assert response.rows == ()


def test_visible_text_candidates_substring_and_span(api: MediaQueryApiV2) -> None:
    by_text = api.visible_text_candidates(
        vm.VisibleTextCandidatesRequest(text_query="spec", pagination=_page())
    )
    assert [row.text for row in by_text.rows] == ["SPEC SHEET"]
    assert by_text.rows[0].shot_id == "shot-a"
    assert by_text.rows[0].frame == 20

    by_span = api.visible_text_candidates(
        vm.VisibleTextCandidatesRequest(
            span=vm.FrameSpan(start_frame=150, end_frame=300), pagination=_page()
        )
    )
    assert [row.text for row in by_span.rows] == ["INTERVIEW"]


def test_visual_quality_ranges_flag_and_span(api: MediaQueryApiV2) -> None:
    by_flag = api.visual_quality_ranges(
        vm.VisualQualityRangesRequest(flags=("blur",), pagination=_page())
    )
    assert [row.shot_id for row in by_flag.rows] == ["shot-a"]
    assert by_flag.rows[0].flag == "blur"

    by_span = api.visual_quality_ranges(
        vm.VisualQualityRangesRequest(
            span=vm.FrameSpan(start_frame=150, end_frame=300), pagination=_page()
        )
    )
    assert [(row.shot_id, row.flag) for row in by_span.rows] == [
        ("shot-c", "black"),
        ("shot-c", "noise"),
    ]


def test_audio_energy_ranges_band_filter(api: MediaQueryApiV2) -> None:
    high = api.audio_energy_ranges(
        vm.AudioEnergyRangesRequest(min_energy=0.5, max_energy=1.0, pagination=_page())
    )
    assert [row.shot_id for row in high.rows] == ["shot-b"]
    assert high.rows[0].energy == pytest.approx(0.8)
    assert high.rows[0].ambient_type == "speech"

    low = api.audio_energy_ranges(
        vm.AudioEnergyRangesRequest(min_energy=0.0, max_energy=0.5, pagination=_page())
    )
    assert [row.shot_id for row in low.rows] == ["shot-a"]


def test_scene_summary_counts_and_lineage(api: MediaQueryApiV2) -> None:
    response = api.scene_summary(vm.SceneSummaryRequest())
    assert response.total == 1
    summary = response.rows[0]
    assert summary.episode_id == EPISODE_ID
    assert summary.source_count == 1
    assert summary.shot_count == 3
    assert summary.covered_frames == 300
    assert {count.name: count.count for count in summary.role_counts} == {
        "b_roll": 2,
        "talking_head": 1,
    }
    assert summary.artifact_shas == (artifact_content_sha(ARTIFACT),)


# ---------------------------------------------------------------- budgets


def test_budget_exceeded_on_oversized_requests(tmp_path: Path) -> None:
    path = build_index(_bulk_artifact(600), tmp_path / "bulk.duckdb")
    with MediaQueryApiV2.open(path) as api:
        with pytest.raises(vm.ApiBudgetExceededV2) as oversized:
            api.shots(
                vm.ShotsRequest(
                    span=vm.FrameSpan(start_frame=0, end_frame=10**9), pagination=_page()
                )
            )
        assert "budget-exceeded" in str(oversized.value)
        with pytest.raises(vm.ApiBudgetExceededV2):
            api.semantic_shot_search(
                vm.SemanticSearchRequest(text_query="bulk", pagination=_page())
            )


def test_pagination_cap_enforced(api: MediaQueryApiV2) -> None:
    with pytest.raises(ValidationError):
        vm.V2Pagination(limit=vm.V2_MAX_PAGE_SIZE + 1, offset=0)
    with pytest.raises(vm.ApiBudgetExceededV2):
        api.shots(
            vm.ShotsRequest(
                span=vm.FrameSpan(start_frame=0, end_frame=300),
                pagination=vm.V2Pagination(limit=vm.V2_MAX_PAGE_SIZE, offset=496),
            )
        )


# ---------------------------------------------------------------- contracts


def test_queries_never_mutate_index_bytes_or_mtime(index_path: Path) -> None:
    before_bytes = hashlib.sha256(index_path.read_bytes()).hexdigest()
    before_mtime = index_path.stat().st_mtime_ns
    with MediaQueryApiV2.open(index_path) as api:
        api.shots(vm.ShotsRequest(span=vm.FrameSpan(start_frame=0, end_frame=300),
                                  pagination=_page()))
        api.shot_detail(vm.ShotDetailRequest(shot_id="shot-a"))
        api.best_moments(vm.BestMomentsRequest(pagination=_page()))
        api.transcript_range(
            vm.TranscriptRangeRequest(
                source_id=SOURCE_ID,
                span=vm.FrameSpan(start_frame=0, end_frame=300),
                pagination=_page(),
            )
        )
        api.semantic_shot_search(vm.SemanticSearchRequest(text_query="製品", pagination=_page()))
        api.similar_shots(vm.SimilarShotsRequest(shot_id="shot-a"))
        api.visible_text_candidates(vm.VisibleTextCandidatesRequest(pagination=_page()))
        api.visual_quality_ranges(vm.VisualQualityRangesRequest(pagination=_page()))
        api.audio_energy_ranges(vm.AudioEnergyRangesRequest(pagination=_page()))
        api.scene_summary(vm.SceneSummaryRequest())
        with pytest.raises(vm.ApiShotNotFoundV2):
            api.shot_detail(vm.ShotDetailRequest(shot_id="shot-missing"))
    after_bytes = hashlib.sha256(index_path.read_bytes()).hexdigest()
    assert after_bytes == before_bytes
    assert index_path.stat().st_mtime_ns == before_mtime


def test_unknown_shot_id_is_typed_not_found(api: MediaQueryApiV2) -> None:
    with pytest.raises(vm.ApiShotNotFoundV2) as missing:
        api.shot_detail(vm.ShotDetailRequest(shot_id="shot-missing"))
    assert missing.value.shot_id == "shot-missing"
    with pytest.raises(vm.ApiShotNotFoundV2):
        api.similar_shots(vm.SimilarShotsRequest(shot_id="shot-missing"))


def test_inverted_span_is_rejected_at_request_boundary(api: MediaQueryApiV2) -> None:
    with pytest.raises(ValidationError):
        vm.FrameSpan(start_frame=300, end_frame=100)
    with pytest.raises(ValidationError):
        vm.AudioEnergyRangesRequest(min_energy=-1.0, pagination=_page())


def test_rebuild_is_deterministic(tmp_path: Path) -> None:
    first = build_index(ARTIFACT, tmp_path / "rebuild-1.duckdb")
    second = build_index(ARTIFACT, tmp_path / "rebuild-2.duckdb")
    with MediaQueryApiV2.open(first) as api_one, MediaQueryApiV2.open(second) as api_two:
        span = vm.FrameSpan(start_frame=0, end_frame=300)
        shots_one = api_one.shots(vm.ShotsRequest(span=span, pagination=_page()))
        shots_two = api_two.shots(vm.ShotsRequest(span=span, pagination=_page()))
        assert shots_one == shots_two
        assert api_one.scene_summary(vm.SceneSummaryRequest()) == api_two.scene_summary(
            vm.SceneSummaryRequest()
        )
        query = vm.SemanticSearchRequest(text_query="製品 軽量", pagination=_page())
        assert api_one.semantic_shot_search(query) == api_two.semantic_shot_search(query)
