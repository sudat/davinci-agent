"""Evidence v2: multimodal candidate corroboration over MediaQueryApiV2 (task 23).

Builds the synthetic v2 media-intelligence index via ``index_v2.build_index``
(task 15), then proves: each of the seven PRD 7.4 corroboration methods
corroborates the right candidate types (exact method tuples); vision evidence
corroborates a b_roll candidate; a cited nonexistent shot id raises
``EvidenceIncompleteV2`` naming it; api budget exhaustion raises the typed
budget error (never a silent partial bundle); partial bundles appear only
under an explicit allowance; and every ref the bundle records re-queries to
a real index id (NO-INVENTED-IDS).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from services.editorial_v2.evidence_v2 import (
    LEGACY_METHOD_ALIASES,
    CandidateEvidenceV2,
    EvidenceBudgetExceededV2,
    EvidenceBundleV2,
    EvidenceIncompleteV2,
    assemble_evidence_v2,
)
from services.editorial_v2.moment_models import (
    MomentCandidateType,
    MomentCandidateV2,
    MomentProvenance,
    MomentSourceSpan,
)
from services.media_intelligence.models import (
    AudioMeasurements,
    EditSourceSpan,
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
)
from services.media_intelligence.moment_review import review_content_sha
from services.media_query import v2_models as vm
from services.media_query.index_v2 import build_index
from services.media_query.query_v2 import MediaQueryApiV2
from tests.editorial_v2.fixtures.three_pass_fixture import make_moment_review

SOURCE_ID = "src-cam-a"
EPISODE_ID = "ep-001"


def _shot(  # noqa: PLR0913 (synthetic fixture table: one kwarg per indexed evidence kind)
    shot_id: str,
    start: int,
    end: int,
    *,
    description: str,
    role: str,
    potential: str = "medium",
    shot_size: str = "medium",
    flags: tuple[str, ...] = (),
    transcripts: tuple[tuple[str, str, int, int], ...] = (),
    audio: AudioMeasurements | None = None,
    similarity: tuple[tuple[str, float], ...] = (),
) -> Shot:
    return Shot(
        shot_id=shot_id,
        source_span=EditSourceSpan(start_frame=start, end_frame=end),
        description=description,
        visual=ShotVisual(shot_size=shot_size, camera_motion="static", quality_flags=flags),
        editorial=ShotEditorial(
            role=role,
            select_potential=potential,
            best_moment=ShotBestMoment(frame=(start + end) // 2, why="決定瞬間"),
            pacing="moderate",
            cuttability=ShotCuttability.model_validate({"in": "clean", "out": "clean"}),
        ),
        transcript_segments=tuple(
            TranscriptSegment(segment_id=seg_id, text=text, start_frame=s, end_frame=e)
            for seg_id, text, s, e in transcripts
        )
        or None,
        audio_measurements=audio,
        similarity_refs=tuple(
            SimilarityRef(ref_shot_id=ref, score=score) for ref, score in similarity
        )
        or None,
        confidence=ShotConfidence(editorial="medium", visual="high"),
    )


SHOT_A = _shot(
    "shot-a",
    0,
    100,
    description="商品を手に取って軽量な製品を説明する close-up demo",
    role="b_roll",
    shot_size="close_up",
    flags=("blur",),
    transcripts=(("tr-a1", "この製品は軽量です", 10, 90),),
    audio=AudioMeasurements(loudness_db=-18.5, energy=0.4, ambient_type="room"),
    similarity=(("shot-x1", 0.9),),
)
SHOT_B = _shot(
    "shot-b",
    100,
    200,
    description="two people talking about editing workflow",
    role="talking_head",
    potential="high",
    transcripts=(("tr-b1", "今日は編集の話をします", 110, 190),),
    audio=AudioMeasurements(loudness_db=-12.0, energy=0.8, ambient_type="speech"),
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
)

ARTIFACT = MediaIntelligenceArtifact(
    episode_id=EPISODE_ID,
    sources=(MediaSource(source_id=SOURCE_ID, duration_frames=300),),
    shots=(SHOT_A, SHOT_B, SHOT_C),
)


@pytest.fixture
def api(tmp_path: Path) -> Iterator[MediaQueryApiV2]:
    path = build_index(ARTIFACT, tmp_path / "mi-v2.duckdb")
    with MediaQueryApiV2.open(path) as opened:
        yield opened


FUSED_REVIEW = make_moment_review("ep-001", 0, 150, overall=0.9, source_duration=300)
FUSED_SHA = review_content_sha(FUSED_REVIEW)


@pytest.fixture
def api_fused(tmp_path: Path) -> Iterator[MediaQueryApiV2]:
    """The same index rebuilt with one fused deep review over [0, 150)."""
    path = build_index(ARTIFACT, tmp_path / "mi-v2-fused.duckdb", reviews=(FUSED_REVIEW,))
    with MediaQueryApiV2.open(path) as opened:
        yield opened


def _cand(
    candidate_id: str,
    candidate_type: MomentCandidateType,
    start: int,
    end: int,
    refs: tuple[str, ...],
) -> MomentCandidateV2:
    return MomentCandidateV2(
        candidate_id=candidate_id,
        candidate_type=candidate_type,
        source_span=MomentSourceSpan(start_frame=start, end_frame=end),
        intent="keep",
        rationale="索引された証拠と一致する候補",
        evidence_refs=refs,
        confidence=0.8,
        provenance=MomentProvenance(producer="test-suite"),
    )


# ------------------------------------------------------------ happy paths


@pytest.mark.parametrize(
    ("candidate_type", "start", "end", "refs", "expected_methods"),
    [
        ("speech", 10, 90, ("shot-a", "tr-a1"), ("transcript",)),
        ("b_roll", 0, 100, ("shot-a",), ("exact_frames",)),
        ("pause", 0, 100, ("shot-a",), ("audio_measurement",)),
        ("alternate_take", 0, 100, ("shot-a",), ("similarity",)),
        ("establishing", 200, 300, ("shot-c",), ("scene_metadata",)),
        ("ambient", 100, 200, ("shot-b",), ("audio_measurement", "scene_metadata")),
        ("graphic", 200, 300, ("shot-c",), ("exact_frames", "source_quality")),
    ],
)
def test_each_method_corroborates_happy_path(  # noqa: PLR0913, PLR0917 (parametrized case table)
    api: MediaQueryApiV2,
    candidate_type: MomentCandidateType,
    start: int,
    end: int,
    refs: tuple[str, ...],
    expected_methods: tuple[str, ...],
) -> None:
    """Without indexed fused reviews, deep_shot_vision NEVER hits — shot
    descriptions are no longer relabeled as deep vision (T7)."""

    bundle = assemble_evidence_v2(
        api, [_cand("cand-x", candidate_type, start, end, refs)], source_id=SOURCE_ID
    )
    assert len(bundle.entries) == 1
    assert bundle.entries[0].methods == expected_methods
    assert bundle.entries[0].moment_reviews == ()
    assert bundle.partial is False


def test_deep_shot_vision_cites_overlapping_fused_review(api_fused: MediaQueryApiV2) -> None:
    """deep_shot_vision corroborates ONLY through overlapping fused
    moment-review rows and cites their hash, exact span, and confidence."""

    bundle = assemble_evidence_v2(
        api_fused, [_cand("cand-broll", "b_roll", 0, 100, ("shot-a",))]
    )
    entry = bundle.entries[0]
    assert entry.methods == ("deep_shot_vision", "exact_frames")
    assert len(entry.moment_reviews) == 1
    citation = entry.moment_reviews[0]
    assert citation.review_id == FUSED_REVIEW.review_id
    assert citation.artifact_sha == FUSED_SHA
    assert (citation.span.start_frame, citation.span.end_frame) == (0, 150)
    assert citation.overall_confidence == pytest.approx(0.9)
    assert citation.review_id in entry.evidence_refs
    assert FUSED_SHA in entry.lineage
    assert FUSED_SHA in bundle.lineage


def test_non_overlapping_fused_review_is_never_cited(api_fused: MediaQueryApiV2) -> None:
    """A fused review that does not overlap the candidate span cannot
    corroborate it — exact half-open overlap, no proximity assumptions."""

    bundle = assemble_evidence_v2(
        api_fused, [_cand("cand-late", "b_roll", 200, 300, ("shot-c",))]
    )
    entry = bundle.entries[0]
    assert "deep_shot_vision" not in entry.methods
    assert entry.moment_reviews == ()
    assert FUSED_SHA not in entry.lineage


def test_synthetic_overlapping_review_does_not_corroborate(tmp_path: Path) -> None:
    """A synthetic overlapping review must not satisfy deep_shot_vision —
    the T7 contract requires genuine fused evidence, not placeholders."""

    synthetic = make_moment_review(
        "ep-001", 0, 150, overall=0.9, source_duration=300, provider="synthetic"
    )
    path = build_index(ARTIFACT, tmp_path / "synthetic.duckdb", reviews=(synthetic,))
    with MediaQueryApiV2.open(path) as api:
        bundle = assemble_evidence_v2(api, [_cand("cand-broll", "b_roll", 0, 100, ("shot-a",))])
        entry = bundle.entries[0]
        assert "deep_shot_vision" not in entry.methods
        assert entry.moment_reviews == ()
        assert review_content_sha(synthetic) not in entry.lineage


def test_legacy_non_fused_review_does_not_corroborate(tmp_path: Path) -> None:
    """A real legacy row whose tool is not the T6 fusion identity must not
    satisfy deep_shot_vision merely because it overlaps."""

    legacy = make_moment_review(
        "ep-001",
        0,
        150,
        overall=0.9,
        source_duration=300,
        provider="openai",
        tool="multimodal-v1",
        provider_version="gpt-5.6-sol",
    )
    path = build_index(ARTIFACT, tmp_path / "legacy.duckdb", reviews=(legacy,))
    with MediaQueryApiV2.open(path) as api:
        bundle = assemble_evidence_v2(api, [_cand("cand-broll", "b_roll", 0, 100, ("shot-a",))])
        entry = bundle.entries[0]
        assert "deep_shot_vision" not in entry.methods
        assert entry.moment_reviews == ()
        assert review_content_sha(legacy) not in entry.lineage


def test_fused_review_page_is_cached_per_span(api_fused: MediaQueryApiV2) -> None:
    """The moment-review query rides the same per-span caching discipline as
    the shots page: shared spans cost one review call, not one per method."""

    bundle = assemble_evidence_v2(
        api_fused, [_cand("cand-broll", "b_roll", 0, 100, ("shot-a",))]
    )
    assert bundle.budget.api_calls == 2  # moment_reviews + shots (cached across methods)
    assert bundle.budget.rows_returned == 2  # one review row + one shot row


def test_legacy_method_aliases_map_to_legal_v2_methods() -> None:
    assert LEGACY_METHOD_ALIASES == {
        "transcript_search": "transcript",
        "silence_overlap": "audio_measurement",
    }
    for modern in LEGACY_METHOD_ALIASES.values():
        record = CandidateEvidenceV2(
            candidate_id="cand-alias",
            methods=(modern,),
            evidence_refs=("shot-a",),
            lineage=("a" * 64,),
        )
        assert record.methods == (modern,)


# ------------------------------------------------------------ failure paths


def test_nonexistent_shot_ref_raises_incomplete_naming_it(api: MediaQueryApiV2) -> None:
    ghost = _cand("cand-ghost", "b_roll", 0, 100, ("shot-a", "shot-ghost"))
    with pytest.raises(EvidenceIncompleteV2) as error:
        assemble_evidence_v2(api, [ghost])
    assert "shot-ghost" in str(error.value)
    assert "cand-ghost" in error.value.detail


def test_budget_exhaustion_raises_typed_budget_error(tmp_path: Path) -> None:
    bulk = MediaIntelligenceArtifact(
        episode_id="ep-bulk",
        sources=(MediaSource(source_id="src-bulk", duration_frames=6000),),
        shots=tuple(
            _shot(
                f"shot-bulk-{index:04d}",
                index * 10,
                index * 10 + 10,
                description=f"bulk filler shot {index}",
                role="coverage",
                potential="low",
            )
            for index in range(600)
        ),
    )
    path = build_index(bulk, tmp_path / "bulk.duckdb")
    with MediaQueryApiV2.open(path) as api:
        wide = _cand("cand-wide", "b_roll", 0, 6000, ("shot-bulk-0000",))
        with pytest.raises(EvidenceBudgetExceededV2) as error:
            assemble_evidence_v2(api, [wide])
        assert error.value.code == "budget-exceeded"
        assert isinstance(error.value, EvidenceIncompleteV2)


def test_uncorroborated_strict_by_default_partial_only_with_allowance(
    api: MediaQueryApiV2,
) -> None:
    good = _cand("cand-good", "b_roll", 0, 100, ("shot-a",))
    dead = _cand("cand-dead", "b_roll", 310, 320, ("shot-a",))
    with pytest.raises(EvidenceIncompleteV2) as error:
        assemble_evidence_v2(api, [good, dead])
    assert "cand-dead" in error.value.detail
    bundle = assemble_evidence_v2(api, [good, dead], partial_allowance=frozenset({"cand-dead"}))
    assert bundle.partial is True
    assert bundle.missing == ("cand-dead",)
    assert tuple(entry.candidate_id for entry in bundle.entries) == ("cand-good",)


# ------------------------------------------------------------ contracts


def test_every_bundle_ref_requeries_to_a_real_index_id(api: MediaQueryApiV2) -> None:
    candidates = [
        _cand("cand-speech", "speech", 10, 90, ("shot-a", "tr-a1")),
        _cand("cand-broll", "b_roll", 0, 100, ("shot-a",)),
        _cand("cand-alt", "alternate_take", 0, 100, ("shot-a",)),
        _cand("cand-graphic", "graphic", 200, 300, ("shot-c",)),
    ]
    bundle = assemble_evidence_v2(api, candidates, source_id=SOURCE_ID)
    real: set[str] = set()
    page = vm.V2Pagination(limit=50, offset=0)
    full = vm.FrameSpan(start_frame=0, end_frame=300)
    shots = api.shots(vm.ShotsRequest(span=full, pagination=page))
    for row in shots.rows:
        real.add(row.shot_id)
        for similar in api.similar_shots(vm.SimilarShotsRequest(shot_id=row.shot_id)).rows:
            real.add(similar.ref_shot_id)
    transcripts = api.transcript_range(
        vm.TranscriptRangeRequest(source_id=SOURCE_ID, span=full, pagination=page)
    )
    real.update(row.segment_id for row in transcripts.rows)
    real.update(row.episode_id for row in api.scene_summary(vm.SceneSummaryRequest()).rows)
    alt = bundle.entries[2]
    assert "shot-x1" in alt.evidence_refs
    for entry in bundle.entries:
        for ref in entry.evidence_refs:
            assert ref in real


def test_bundle_round_trip_json_and_budget_fields(api_fused: MediaQueryApiV2) -> None:
    bundle = assemble_evidence_v2(
        api_fused,
        [
            _cand("cand-speech", "speech", 10, 90, ("shot-a", "tr-a1")),
            _cand("cand-est", "establishing", 200, 300, ("shot-c",)),
        ],
        source_id=SOURCE_ID,
    )
    restored = EvidenceBundleV2.model_validate(bundle.model_dump(mode="json"))
    assert restored == bundle
    assert bundle.partial is False
    assert bundle.missing == ()
    assert bundle.budget.row_budget == vm.V2_ROW_BUDGET
    assert bundle.budget.api_calls >= 2
    assert bundle.budget.rows_returned >= 2
    fused_entry = restored.entries[0]
    assert fused_entry.moment_reviews == bundle.entries[0].moment_reviews
