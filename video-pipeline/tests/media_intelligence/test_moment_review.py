"""MomentDeepReviewV1 (PRD 7.6, implementation plan 6.6): evidence-only deep review.

Proves: deterministic identity (same episode+window -> same review id and
byte-identical canonical output across double runs); typed rejections for
windows beyond the source, frames outside the window, and unknown neighbour
shots; a full synthetic-provider round with lineage; the static no-mutation
contract (the module exposes no plan/timeline write symbols); and that
committed reviews are retrievable through the additive v2 query path.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.foundation_io import canonical_model_bytes
from services.media_intelligence import moment_review as moment_review_module
from services.media_intelligence.models import (
    EditSourceSpan,
    MediaIntelligenceArtifact,
    MediaSource,
    Shot,
    ShotBestMoment,
    ShotConfidence,
    ShotCuttability,
    ShotEditorial,
    ShotVisual,
)
from services.media_intelligence.moment_review import (
    FrameBundleEntry,
    FrameOutsideWindowError,
    MomentDeepReviewV1,
    NeighboringContext,
    ReviewEvidence,
    ReviewExecutionContext,
    ReviewLineage,
    ReviewProviders,
    ReviewRecordRequest,
    ReviewWindow,
    SyntheticAudioContext,
    SyntheticFrameExtractor,
    SyntheticTranscriptLookup,
    TranscriptRef,
    UnknownNeighborError,
    WindowOutOfBoundsError,
    derive_moment_review_id,
    execute_review,
    record_review,
    review_content_sha,
    review_rows,
)
from services.media_query import v2_models as vm
from services.media_query.index_v2 import build_index
from services.media_query.query_v2 import MediaQueryApiV2

EPISODE_ID = "ep-001"
DURATION_FRAMES = 300
KNOWN_SHOTS = frozenset({"shot-a", "shot-b", "shot-c"})


def _providers() -> ReviewProviders:
    return ReviewProviders(
        frames=SyntheticFrameExtractor(density=4),
        transcripts=SyntheticTranscriptLookup(
            segments=(
                TranscriptRef(segment_id="tr-a1", start_frame=10, end_frame=90),
                TranscriptRef(segment_id="tr-b1", start_frame=110, end_frame=190),
            )
        ),
        audio=SyntheticAudioContext(),
        lineage=ReviewLineage(provider="synthetic-local", provider_version="1",
                              tool="moment-review-synthetic"),
    )


def _context(
    episode_id: str = EPISODE_ID,
    neighbors: NeighboringContext | None = None,
) -> ReviewExecutionContext:
    return ReviewExecutionContext(
        episode_id=episode_id,
        source_duration_frames=DURATION_FRAMES,
        known_shot_ids=KNOWN_SHOTS,
        neighbors=neighbors,
    )


def _record_request(
    window: ReviewWindow,
    *,
    frame_bundle: tuple[FrameBundleEntry, ...] | None = None,
    neighboring_context: NeighboringContext | None = None,
) -> ReviewRecordRequest:
    """Direct builder input for validation tests (bypasses the executor)."""

    review = execute_review(window, _providers(), context=_context())
    evidence = ReviewEvidence(
        frame_bundle=frame_bundle if frame_bundle is not None else review.evidence.frame_bundle,
        transcript_refs=review.evidence.transcript_refs,
        audio_context=review.evidence.audio_context,
    )
    neighbors = neighboring_context or NeighboringContext()
    return ReviewRecordRequest(
        episode_id=EPISODE_ID,
        source_duration_frames=DURATION_FRAMES,
        known_shot_ids=KNOWN_SHOTS,
        window=window,
        neighboring_context=neighbors,
        evidence=evidence,
        assessment=review.assessment,
        confidence=review.confidence,
        lineage=review.lineage,
    )


# --------------------------------------------------------- identity


def test_same_window_double_run_yields_identical_id_and_bytes() -> None:
    window = ReviewWindow(start_frame=100, end_frame=200)
    first = execute_review(window, _providers(), context=_context())
    second = execute_review(window, _providers(), context=_context())

    assert first.review_id == second.review_id
    assert first.review_id == derive_moment_review_id(EPISODE_ID, 100, 200)
    assert canonical_model_bytes(first) == canonical_model_bytes(second)

    other_episode = execute_review(window, _providers(), context=_context(episode_id="ep-002"))
    assert other_episode.review_id != first.review_id
    other_window = execute_review(
        ReviewWindow(start_frame=100, end_frame=201), _providers(), context=_context()
    )
    assert other_window.review_id != first.review_id


def test_window_at_exact_source_bounds_is_accepted() -> None:
    review = execute_review(
        ReviewWindow(start_frame=0, end_frame=DURATION_FRAMES), _providers(), context=_context()
    )
    assert review.source_window == ReviewWindow(start_frame=0, end_frame=DURATION_FRAMES)


# ----------------------------------------------------- typed rejections


def test_window_beyond_source_bounds_is_typed_rejection() -> None:
    with pytest.raises(WindowOutOfBoundsError):
        execute_review(
            ReviewWindow(start_frame=0, end_frame=DURATION_FRAMES + 1),
            _providers(),
            context=_context(),
        )
    with pytest.raises(WindowOutOfBoundsError):
        record_review(_record_request(ReviewWindow(start_frame=250, end_frame=310)))


def test_frame_outside_window_is_typed_rejection() -> None:
    window = ReviewWindow(start_frame=100, end_frame=200)
    at_exclusive_end = (FrameBundleEntry(frame=200, ref="synthetic://frame/200"),)
    with pytest.raises(FrameOutsideWindowError):
        record_review(_record_request(window, frame_bundle=at_exclusive_end))
    before_start = (FrameBundleEntry(frame=99, ref="synthetic://frame/99"),)
    with pytest.raises(FrameOutsideWindowError):
        record_review(_record_request(window, frame_bundle=before_start))


def test_unknown_neighbor_shot_is_typed_rejection() -> None:
    window = ReviewWindow(start_frame=100, end_frame=200)
    with pytest.raises(UnknownNeighborError):
        record_review(
            _record_request(
                window,
                neighboring_context=NeighboringContext(prev_shot_id="shot-ghost"),
            )
        )
    with pytest.raises(UnknownNeighborError):
        record_review(
            _record_request(
                window,
                neighboring_context=NeighboringContext(next_shot_id="shot-ghost"),
            )
        )


# ------------------------------------------------------ synthetic round


def test_synthetic_provider_round_produces_full_review_with_lineage() -> None:
    window = ReviewWindow(start_frame=100, end_frame=200)
    neighbors = NeighboringContext(prev_shot_id="shot-a", next_shot_id="shot-c")
    review = execute_review(window, _providers(), context=_context(neighbors=neighbors))

    assert isinstance(review, MomentDeepReviewV1)
    assert review.schema_version == "moment-deep-review-v1"
    assert review.episode_id == EPISODE_ID
    assert review.source_window == window
    assert review.neighboring_context == neighbors

    frames = [entry.frame for entry in review.evidence.frame_bundle]
    assert frames
    assert all(window.start_frame <= frame < window.end_frame for frame in frames)
    assert len(review.evidence.transcript_refs) == 1
    assert review.evidence.transcript_refs[0] == "tr-b1"
    assert review.evidence.audio_context.note is not None

    assessment = review.assessment
    assert assessment.subject_action_evolution
    assert assessment.reaction_notes
    assert assessment.timing_notes
    sub = assessment.best_sub_span
    assert window.start_frame <= sub.start_frame <= sub.end_frame <= window.end_frame
    assert isinstance(assessment.keep_rationale_candidates, tuple)
    assert isinstance(assessment.remove_rationale_candidates, tuple)
    assert assessment.cut_in_handle
    assert assessment.cut_out_handle

    assert 0.0 <= review.confidence.overall <= 1.0
    assert review.confidence.best_sub_span >= 0.0
    assert review.lineage.provider == "synthetic-local"
    assert review.lineage.provider_version
    assert review.lineage.tool
    assert review.lineage.stage_lineage == ()  # additive T6 field, empty by default


def test_legacy_lineage_payload_without_stage_lineage_parses_with_default() -> None:
    """Pre-T6 lineage payloads (no ``stage_lineage`` key) stay loadable with
    the explicit empty-stage default; a stage-bearing payload round-trips."""

    legacy = {"provider": "synthetic-local", "provider_version": "1",
              "tool": "moment-review-synthetic"}
    lineage = ReviewLineage.model_validate(legacy)
    assert lineage.stage_lineage == ()
    assert lineage.cost is None

    staged = ReviewLineage.model_validate(
        {
            **legacy,
            "stage_lineage": [
                {
                    "purpose": "local_map",
                    "provider": "google-gemini-developer-api-generate-content-rest-v1beta",
                    "model_id": "gemini-3.7-flash",
                    "pin_sha256": "a" * 64,
                    "tool": "video-understanding-v1",
                    "requested_start_frame": 0,
                    "requested_end_frame": 40,
                    "analyzed_start_frame": 0,
                    "analyzed_end_frame": 40,
                    "input_sha256": "b" * 64,
                    "output_sha256": "c" * 64,
                    "attempts": 1,
                    "outcome": "analyzed",
                }
            ],
        }
    )
    assert staged.stage_lineage[0].purpose == "local_map"
    assert staged.stage_lineage[0].cost is None

    with pytest.raises(ValidationError):
        ReviewLineage.model_validate(
            {
                **legacy,
                "stage_lineage": [
                    {
                        "purpose": "local_map",
                        "provider": "p",
                        "model_id": "m",
                        "pin_sha256": "a" * 64,
                        "tool": "t",
                        "requested_start_frame": 0,
                        "requested_end_frame": 40,
                        "analyzed_start_frame": 0,
                        "analyzed_end_frame": 41,
                        "input_sha256": "b" * 64,
                        "output_sha256": "c" * 64,
                    }
                ],
            }
        )


def test_review_rows_keep_the_primary_provider_columns() -> None:
    """The v2 row shape is unchanged by T6: eleven primary columns, stage
    detail lives only in the artifact hash."""

    review = execute_review(
        ReviewWindow(start_frame=100, end_frame=200), _providers(), context=_context()
    )
    (row,) = review_rows((review,))
    assert len(row) == 11
    assert row[8:11] == ("synthetic-local", "1", "moment-review-synthetic")


def test_known_neighbors_are_attached_verbatim() -> None:
    window = ReviewWindow(start_frame=100, end_frame=200)
    neighbors = NeighboringContext(prev_shot_id="shot-a")
    review = execute_review(window, _providers(), context=_context(neighbors=neighbors))
    assert review.neighboring_context == neighbors
    assert review.neighboring_context.next_shot_id is None


# -------------------------------------------------- static no-mutation


def test_module_exposes_no_plan_or_timeline_mutation_api() -> None:
    module = moment_review_module
    source = inspect.getsource(module)
    forbidden_defs = re.compile(
        r"^(\s*)?(async )?def (mutate|apply|commit|write|insert|update|delete|patch)\w*",
        re.MULTILINE,
    )
    assert forbidden_defs.search(source) is None
    assert "services.editorial" not in source
    assert "services.resolve" not in source
    defined_here = {
        name
        for name in dir(module)
        if not name.startswith("_")
        and callable(getattr(module, name))
        and getattr(getattr(module, name), "__module__", None) == module.__name__
    }
    assert defined_here == set(module.__all__)


# ---------------------------------------------------- v2 queryability


def _mini_artifact() -> MediaIntelligenceArtifact:
    def shot(shot_id: str, start: int, end: int) -> Shot:
        return Shot(
            shot_id=shot_id,
            source_span=EditSourceSpan(start_frame=start, end_frame=end),
            description=f"synthetic shot {shot_id}",
            visual=ShotVisual(shot_size="medium", camera_motion="static"),
            editorial=ShotEditorial(
                role="talking_head",
                select_potential="medium",
                best_moment=ShotBestMoment(frame=(start + end) // 2, why="synthetic"),
                pacing="moderate",
                cuttability=ShotCuttability.model_validate({"in": "clean", "out": "clean"}),
            ),
            confidence=ShotConfidence(editorial="medium", visual="medium"),
        )

    return MediaIntelligenceArtifact(
        episode_id=EPISODE_ID,
        sources=(MediaSource(source_id="src-cam-a", duration_frames=DURATION_FRAMES),),
        shots=(shot("shot-a", 0, 100), shot("shot-b", 100, 200), shot("shot-c", 200, 300)),
    )


def _page() -> vm.V2Pagination:
    return vm.V2Pagination(limit=50, offset=0)


def test_reviews_are_queryable_through_the_v2_index(tmp_path: Path) -> None:
    review = execute_review(
        ReviewWindow(start_frame=100, end_frame=200), _providers(), context=_context()
    )
    index_path = build_index(_mini_artifact(), tmp_path / "v2.duckdb", reviews=(review,))
    with MediaQueryApiV2.open(index_path) as api:
        hit = api.moment_reviews(
            vm.MomentReviewsRequest(span=vm.FrameSpan(start_frame=150, end_frame=160),
                                    pagination=_page())
        )
        assert hit.total == 1
        row = hit.rows[0]
        assert row.review_id == review.review_id
        assert row.episode_id == EPISODE_ID
        assert row.span == vm.FrameSpan(start_frame=100, end_frame=200)
        assert row.provider == "synthetic-local"
        assert row.artifact_sha == review_content_sha(review)

        miss = api.moment_reviews(
            vm.MomentReviewsRequest(span=vm.FrameSpan(start_frame=0, end_frame=99),
                                    pagination=_page())
        )
        assert miss.total == 0


def test_legacy_index_without_reviews_reports_zero(tmp_path: Path) -> None:
    index_path = build_index(_mini_artifact(), tmp_path / "legacy.duckdb")
    with MediaQueryApiV2.open(index_path) as api:
        response = api.moment_reviews(
            vm.MomentReviewsRequest(span=vm.FrameSpan(start_frame=0, end_frame=DURATION_FRAMES),
                                    pagination=_page())
        )
        assert response.total == 0
        assert response.rows == ()
