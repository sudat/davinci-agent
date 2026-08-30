"""Unit tests for the real V44-0 arm pipeline (no codex, no media toolchain).

The DirectorV2 llm seam is driven by the deterministic planner applied to
each pass REQUEST (canned payloads that exercise the real model-validation
path in ``director_v2`` — unknown-candidate / uncorroborated-keep guards),
and arm-B fused evidence comes from T6's video-understanding orchestration
driven by structural fakes at the provider seam (map → reduce → fusion with
configurable fusion confidence driving the escalation policy).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import services.cli.v44_product_proof as proof
from services.cli._v44_arm_video_factory import (
    build_video_understanding_factory,
    make_video_http_post,
)
from services.cli.real_pool import SpeechSegment
from services.cli.v44_arm_evidence import (
    ArmEvidenceError,
    build_speech_mi_artifact,
    compose_arm_brief,
    compute_arm_evidence_quality,
    escalated_anchor_ids,
    mezz_span_to_anchor_space,
)
from services.cli.v44_arm_pipeline import (
    ESCALATE_BELOW,
    ArmPipelineInputs,
    ArmPipelineResult,
    run_arm_pipeline,
)
from services.cli.v44_arm_stages import (
    ArmPipelineData,
    ArmPipelineError,
    whisper_provider_pin,
)
from services.cli.v44_product_proof import _runtime_config_path
from services.cli.v44_product_proof import main as cli_main
from services.editorial_v2.editorial_pins import (
    MOMENT_REVIEW_PIN_PATH,
    MOMENT_REVIEW_SPECIALIST_PIN_PATH,
    load_editorial_pin,
    load_editorial_runtime,
)
from services.editorial_v2.heuristic_planner import (
    plan_creative,
    plan_selection,
    plan_story,
)
from services.editorial_v2.prompt_v2 import PassARequest, PassBRequest
from services.media_intelligence.moment_review import (
    ReviewWindow,
    SyntheticAudioContext,
    SyntheticTranscriptLookup,
    review_content_sha,
)
from services.media_intelligence.moment_review_real import (
    RealAudioContext,
    RealTranscriptLookup,
    SyntheticLineageError,
)
from services.media_intelligence.video_clip_evidence import VideoClipEvidence
from services.media_intelligence.video_clip_extraction import ClipEvidencePair, ClipExtractor
from services.media_intelligence.video_review_exchange import WireOutcome
from services.media_intelligence.video_review_wire import (
    GeminiClipReview,
    GlmClipObservation,
    VideoProviderError,
)
from services.media_intelligence.video_stage_wire import (
    FusionConfidence,
    FusionSubSpan,
    GeminiEpisodeReduce,
    GeminiFusionReview,
    GeminiStageResult,
)
from services.media_intelligence.video_understanding import run_video_understanding
from services.media_intelligence.video_understanding_models import VideoUnderstandingDeps
from services.media_query import v2_models as vm
from services.media_query.index_v2 import build_index
from services.media_query.query_v2 import MediaQueryApiV2
from services.metrics.v44_product_proof import (
    EditorialGroundTruthV1,
    EvaluationBindingV1,
    GroundTruthAnchor,
    TranscriptSampleV1,
)
from services.metrics.v44_product_proof import TranscriptSegment as SampleSegment
from tests.editorial_v2.fixtures.three_pass_fixture import make_moment_review

if TYPE_CHECKING:
    from services.cli.v44_arm_pipeline import VideoUnderstandingFactory
    from services.editorial_v2.prompt_v2 import PassName
    from services.media_intelligence.video_stage_wire import GeminiStageResult
    from services.media_intelligence.video_understanding import (
        VideoUnderstandingRequest,
        VideoUnderstandingResult,
    )
def _planner_fake(stage: PassName, request: object) -> object:
    """Canned payloads: the deterministic planner over each pass request."""

    if stage == "pass_a":
        return plan_story(request).model_dump(mode="json")  # type: ignore[arg-type]
    if stage == "pass_b":
        return plan_selection(request, ()).model_dump(mode="json")  # type: ignore[arg-type]
    return plan_creative(request).model_dump(mode="json")  # type: ignore[arg-type]


def _analysis() -> ArmPipelineData:
    speech = (
        SpeechSegment(
            segment_id="s1",
            text="DJI Pocket 4 のケースが無くなった",
            start_frame=0,
            end_frame=90,
        ),
        SpeechSegment(
            segment_id="s2",
            text="部屋中探したけど見つからない",
            start_frame=90,
            end_frame=180,
        ),
        SpeechSegment(
            segment_id="s3", text="また明日探してみる", start_frame=180, end_frame=270
        ),
    )
    return ArmPipelineData(
        episode_id="v44-arm-unit",
        source_id="v44-arm-unit-edit-source",
        total_frames=300,
        speech=speech,
        transcript_segments_ms=(
            (0, 3000, "DJI Pocket 4 のケースが無くなった"),
            (3000, 6000, "部屋中探したけど見つからない"),
            (6000, 9000, "また明日探してみる"),
        ),
        mezzanine=None,
        mezzanine_sha256=None,
    )


def _inputs(tmp_path: Path) -> ArmPipelineInputs:
    analysis = _analysis()
    return ArmPipelineInputs(
        episode_root=tmp_path / "episode",
        workspace=tmp_path / "workspace",
        episode_id=analysis.episode_id,
        brief=compose_arm_brief(tmp_path / "episode", analysis.episode_id, "suda"),
        llm_call=_planner_fake,
        analysis=analysis,
    )


def _eval_binding() -> EvaluationBindingV1:
    return EvaluationBindingV1(
        ground_truth_sha256="a" * 64,
        ground_truth_label="v2",
        evidence_lane="operator_corrected_diagnostic",
        transcript_sha256="b" * 64,
    )


class _VuGemini:
    """Structural fake at the T6 provider seam; logs the orchestration order."""

    def __init__(self, *, fusion_overall: float, fail_local: bool = False) -> None:
        self.pin = load_editorial_pin(MOMENT_REVIEW_PIN_PATH)
        self.fusion_overall = fusion_overall
        self.fail_local = fail_local
        self.events: list[str] = []

    def review_with_trace(
        self, purpose: str, clip: VideoClipEvidence, untrusted_data: str
    ) -> WireOutcome[GeminiStageResult]:
        start, end = clip.requested_range.start_frame, clip.requested_range.end_frame
        self.events.append(f"vu:{purpose}")
        if purpose == "local_map":
            if self.fail_local:
                raise VideoProviderError(
                    "provider-transport", "stable fake local failure", attempts=1
                )
            return WireOutcome(
                GeminiClipReview(
                    analyzed_start_frame=start,
                    analyzed_end_frame=end,
                    summary="arm local summary",
                    observations=("arm observation",),
                    audio_note="arm audio note",
                ),
                1,
            )
        if purpose == "global_reduce":
            return WireOutcome(self._reduce(), 1)
        return WireOutcome(self._fusion(start, end), 1)

    @staticmethod
    def _reduce() -> GeminiEpisodeReduce:
        return GeminiEpisodeReduce(
            analyzed_start_frame=0,
            analyzed_end_frame=300,
            summary="arm reduce summary",
            observations=("arm reduce observation",),
            audio_note="arm reduce audio",
            specialist_requests=(),
        )

    def _fusion(self, start: int, end: int) -> GeminiFusionReview:
        confidence = FusionConfidence(
            overall=self.fusion_overall,
            subject_action_evolution=self.fusion_overall,
            reaction_notes=self.fusion_overall,
            timing_notes=self.fusion_overall,
            best_sub_span=self.fusion_overall,
        )
        return GeminiFusionReview(
            analyzed_start_frame=start,
            analyzed_end_frame=end,
            subject_action_evolution="arm subject evolution",
            reaction_notes="arm reaction notes",
            timing_notes="arm timing notes",
            best_sub_span=FusionSubSpan(start_frame=start + 5, end_frame=end - 5),
            keep_rationale_candidates=("arm keep rationale",),
            remove_rationale_candidates=(),
            cut_in_handle="arm cut in",
            cut_out_handle="arm cut out",
            confidence=confidence,
        )


class _VuSpecialist:
    """No reduce specialist requests in this fixture, so GLM stays uncalled."""

    def __init__(self) -> None:
        self.pin = load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH)
        self.calls: list[tuple[int, int]] = []

    def observe_with_trace(
        self, clip: VideoClipEvidence, untrusted_data: str
    ) -> WireOutcome[GlmClipObservation]:
        start, end = clip.requested_range.start_frame, clip.requested_range.end_frame
        self.calls.append((start, end))
        return WireOutcome(
            GlmClipObservation(
                analyzed_start_frame=start,
                analyzed_end_frame=end,
                visual_findings=("arm visual finding",),
                uncertainty="arm uncertainty",
            ),
            1,
        )


@dataclass(slots=True)
class _VuClips:
    calls: list[tuple[int, int]] = field(default_factory=list)

    def extract(self, window: ReviewWindow) -> ClipEvidencePair:
        start, end = int(window.start_frame), int(window.end_frame)
        self.calls.append((start, end))
        return ClipEvidencePair(
            gemini=self._evidence(start, end, audio_present=True),
            glm=self._evidence(start, end, audio_present=False),
        )

    @staticmethod
    def _evidence(start: int, end: int, *, audio_present: bool) -> VideoClipEvidence:
        digest = sha256(f"arm-fake-clip-{start}-{end}".encode()).hexdigest()
        span = ReviewWindow(start_frame=start, end_frame=end)
        return VideoClipEvidence(
            ref=f"file:///tmp/arm-fake/clip-{start:06d}-{end:06d}.mp4",
            sha256=digest,
            requested_range=span,
            analyzed_range=span,
            audio_present=audio_present,
            duration_seconds=(end - start) / 30.0,
        )


def _vu_deps(
    gemini: _VuGemini,
) -> tuple[VideoUnderstandingDeps, VideoUnderstandingFactory]:
    deps = VideoUnderstandingDeps(
        gemini=gemini,
        specialist=_VuSpecialist(),
        clips=_VuClips(),
        transcripts=SyntheticTranscriptLookup(segments=()),
        audio=SyntheticAudioContext(),
    )
    return deps, lambda _data, _api: deps


def test_arm_b_real_transcript_lookup_stays_usable_through_fused_reviews(
    tmp_path: Path,
) -> None:
    """MEASURED 2026-08-30 (T11 real Arm B run): the production factory
    builds RealTranscriptLookup/RealAudioContext over the MediaQueryApiV2
    opened for the factory call — run_arm_pipeline must keep that
    connection open through the fused reviews (the with-block previously
    closed it before _fused_reviews ran, killing Arm B with
    duckdb Connection-already-closed)."""

    def real_index_factory(
        data: ArmPipelineData, api: MediaQueryApiV2
    ) -> VideoUnderstandingDeps:
        return VideoUnderstandingDeps(
            gemini=_VuGemini(fusion_overall=0.9),
            specialist=_VuSpecialist(),
            clips=_VuClips(),
            transcripts=RealTranscriptLookup(api=api, source_id=data.source_id),
            audio=RealAudioContext(api=api),
        )

    inputs = replace(_inputs(tmp_path), video_understanding=real_index_factory)
    result = run_arm_pipeline(inputs)

    assert result.reviews, "the fused flow must complete over the live index"


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def test_arm_a_kept_spans_derive_from_committed_selection(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    result = run_arm_pipeline(inputs)

    committed = _load_json(inputs.workspace / "moment-selection.json")
    assert isinstance(committed, dict)
    candidates = committed["proposal"]["candidates"]
    expected = tuple(
        (c["source_span"]["start_frame"], c["source_span"]["end_frame"])
        for c in candidates
        if c["intent"] == "keep"
    )
    assert result.kept_spans_mezz == expected
    assert result.kept_spans_mezz, "the committed selection must keep something"
    assert result.escalated_candidate_ids == ()
    assert result.reviews == ()
    assert result.candidate_count == len(candidates)
    for name in (
        "story-plan.json",
        "moment-selection.json",
        "creative-edit.json",
        "media-intelligence.json",
    ):
        assert (inputs.workspace / name).is_file()
    versions = _load_json(inputs.workspace / "moment-selection" / "versions.json")
    assert isinstance(versions, dict)
    assert result.commit_version == len(versions["versions"]) == 2


def test_arm_b_fused_evidence_precedes_director_and_commit(tmp_path: Path) -> None:
    """The real orchestration order: speech MI/index → full-source video
    understanding → index rebuilt with the fused reviews → Director Pass
    A/B/C → validation → exactly one commit."""
    events: list[str] = []
    captured: dict[str, object] = {}
    gemini = _VuGemini(fusion_overall=0.9)
    _deps, factory = _vu_deps(gemini)

    def recording_llm(stage: PassName, request: object) -> object:
        events.append(f"director:{stage}")
        if stage == "pass_b":
            captured["pass_b"] = request
        return _planner_fake(stage, request)

    inputs = replace(_inputs(tmp_path), llm_call=recording_llm, video_understanding=factory)
    result = run_arm_pipeline(inputs)

    order = gemini.events + events
    assert order.index("vu:local_map") < order.index("director:pass_a")
    assert order.index("vu:fusion") < order.index("director:pass_a")
    assert result.reviews, "fused reviews are the arm-B review evidence"
    review = result.reviews[0]
    sha = review_content_sha(review)
    assert (inputs.workspace / "moment-review" / f"{review.review_id}.json").is_file()
    assert next(s.purpose for s in review.lineage.stage_lineage) == "local_map"
    assert result.commit_version == 2
    assert any("fused_reviews=1" in note for note in result.notes)

    pass_b = captured["pass_b"]
    assert isinstance(pass_b, PassBRequest)
    assert sha in pass_b.evidence.lineage
    kept_id = result.kept_candidate_ids[0]
    entry = next(e for e in pass_b.evidence.entries if e.candidate_id == kept_id)
    assert entry.moment_reviews, "the keep cites its overlapping fused review"
    citation = entry.moment_reviews[0]
    assert citation.artifact_sha == sha
    assert (citation.span.start_frame, citation.span.end_frame) == (0, 300)
    assert citation.overall_confidence == pytest.approx(0.9)

    index = inputs.workspace / "media-intelligence.duckdb"
    with MediaQueryApiV2.open(index) as api:
        hit = api.moment_reviews(
            vm.MomentReviewsRequest(
                span=vm.FrameSpan(start_frame=0, end_frame=10),
                pagination=vm.V2Pagination(limit=50, offset=0),
            )
        )
        assert hit.total == 1
        assert hit.rows[0].artifact_sha == sha
        assert hit.rows[0].review_id == review.review_id


def test_arm_b_low_confidence_fused_reviews_demote_and_escalate(tmp_path: Path) -> None:
    gemini = _VuGemini(fusion_overall=ESCALATE_BELOW - 0.1)
    _deps, factory = _vu_deps(gemini)
    inputs = replace(_inputs(tmp_path), video_understanding=factory)
    result = run_arm_pipeline(inputs)

    escalated = result.escalated_candidate_ids
    assert escalated, "low-confidence fused evidence must demote+escalate keeps"
    assert result.kept_spans_mezz == ()
    committed = _load_json(inputs.workspace / "moment-selection.json")
    assert isinstance(committed, dict)
    keeps = [
        c["candidate_id"]
        for c in committed["proposal"]["candidates"]
        if c["intent"] == "keep"
    ]
    assert sorted(escalated) == sorted(keeps)
    assert any("arm_b_escalation" in note for note in result.notes)
    gt = EditorialGroundTruthV1(
        episode_id="v44-arm-unit",
        anchors=(
            GroundTruthAnchor(
                anchor_id="a-1", start_frame=0, end_frame=95, label="must_keep"
            ),
        ),
        created_at="2026-08-24T00:00:00+00:00",
        operator="suda",
    )
    escalated_spans = tuple(
        mezz_span_to_anchor_space(s, e) for s, e in result.escalated_spans_mezz
    )
    assert "a-1" in escalated_anchor_ids(gt.anchors, escalated_spans)


def test_arm_b_high_confidence_fused_reviews_reconfirm_keeps(tmp_path: Path) -> None:
    gemini = _VuGemini(fusion_overall=0.9)
    _deps, factory = _vu_deps(gemini)
    inputs = replace(_inputs(tmp_path), video_understanding=factory)
    result = run_arm_pipeline(inputs)

    assert result.reviews
    assert result.reviews[0].lineage.provider != "synthetic"
    assert result.escalated_candidate_ids == ()
    assert result.kept_spans_mezz, "re-confirmed keeps stay kept"
    assert result.commit_version == 2


def test_arm_b_video_understanding_failure_blocks_before_director_and_commit(
    tmp_path: Path,
) -> None:
    """A coverage/provider failure in video understanding IS the result: the
    Director never proposes and the commit is never called."""

    gemini = _VuGemini(fusion_overall=0.9, fail_local=True)
    _deps, factory = _vu_deps(gemini)
    inputs = replace(_inputs(tmp_path), video_understanding=factory)
    with pytest.raises(VideoProviderError):
        run_arm_pipeline(inputs)
    assert not (inputs.workspace / "moment-selection.json").is_file()
    assert not (inputs.workspace / "moment-selection").exists()
    assert not list((inputs.workspace / "moment-review").glob("*.json"))


def test_arm_b_synthetic_fused_lineage_blocks_before_director_and_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.cli.v44_arm_pipeline as arm_module  # noqa: PLC0415

    gemini = _VuGemini(fusion_overall=0.9)
    _deps, factory = _vu_deps(gemini)

    def synthetic_run(
        request: VideoUnderstandingRequest, wiring: VideoUnderstandingDeps
    ) -> VideoUnderstandingResult:
        result = run_video_understanding(request, wiring)
        return result.model_copy(
            update={
                "reviews": (
                    make_moment_review(
                        "v44-arm-unit", 0, 300, overall=0.9, source_duration=300,
                        provider="synthetic",
                    ),
                )
            }
        )

    monkeypatch.setattr(arm_module, "run_video_understanding", synthetic_run)
    inputs = replace(_inputs(tmp_path), video_understanding=factory)
    with pytest.raises(SyntheticLineageError):
        run_arm_pipeline(inputs)
    assert not (inputs.workspace / "moment-selection.json").is_file()
    assert not (inputs.workspace / "moment-selection").exists()


def test_mezz_to_anchor_space_conversion() -> None:
    assert mezz_span_to_anchor_space(0, 90) == (0, 90)
    assert mezz_span_to_anchor_space(3000, 3009) == (2997, 3006)
    assert mezz_span_to_anchor_space(8469, 8469) == (8461, 8461)


def _sparse_analysis() -> ArmPipelineData:
    """Sparse episode: silent gaps make sum(segment lengths)=270 smaller than
    the authoritative total_frames=400; s3's tail starts beyond the sum."""
    speech = (
        SpeechSegment(
            segment_id="s1", text="DJI Pocket 4 のケースが無くなった", start_frame=0,
            end_frame=90,
        ),
        SpeechSegment(
            segment_id="s2", text="部屋中探したけど見つからない", start_frame=180,
            end_frame=270,
        ),
        SpeechSegment(
            segment_id="s3", text="また明日探してみる", start_frame=270, end_frame=360
        ),
    )
    return ArmPipelineData(
        episode_id="v44-arm-sparse",
        source_id="v44-arm-sparse-edit-source",
        total_frames=400,
        speech=speech,
        transcript_segments_ms=(
            (0, 3000, "DJI Pocket 4 のケースが無くなった"),
            (6000, 9000, "部屋中探したけど見つからない"),
            (9000, 12000, "また明日探してみる"),
        ),
        mezzanine=None,
        mezzanine_sha256=None,
    )


def _sparse_inputs(tmp_path: Path) -> ArmPipelineInputs:
    analysis = _sparse_analysis()
    return ArmPipelineInputs(
        episode_root=tmp_path / "episode",
        workspace=tmp_path / "workspace",
        episode_id=analysis.episode_id,
        brief=compose_arm_brief(tmp_path / "episode", analysis.episode_id, "suda"),
        llm_call=_planner_fake,
        analysis=analysis,
    )


def test_sparse_tail_candidates_reach_director_and_commit(tmp_path: Path) -> None:
    """Task-2 sparse-extent contract: production Arm discovery uses the
    authoritative ``ArmPipelineData.total_frames``, so a tail speech segment
    beyond the summed coverage still becomes a candidate, reaches the
    committed selection, and the Pass A digest carries the extent."""
    captured: dict[str, object] = {}

    def capturing_llm(stage: PassName, request: object) -> object:
        captured[stage] = request
        return _planner_fake(stage, request)

    inputs = replace(_sparse_inputs(tmp_path), llm_call=capturing_llm)
    result = run_arm_pipeline(inputs)

    assert result.candidate_count == 3
    committed = _load_json(inputs.workspace / "moment-selection.json")
    assert isinstance(committed, dict)
    ids = {c["candidate_id"] for c in committed["proposal"]["candidates"]}
    assert ids == {"cand-s1", "cand-s2", "cand-s3"}
    pass_a = captured["pass_a"]
    assert isinstance(pass_a, PassARequest)
    assert pass_a.evidence_digest.source_total_frames == 400
    assert pass_a.evidence_digest.covered_frames == 360  # max discovered end
    assert pass_a.evidence_digest.shot_count == 3


def test_equal_count_id_substitution_refuses_before_paid_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One indexed shot ID substituted (count preserved) is a typed
    ``candidate-integrity-failed`` refusal naming BOTH the missing and the
    extra ID — raised before video understanding (Gemini/GLM), before the
    Director llm seam (codex), and before any selection output."""
    import services.cli.v44_arm_pipeline as arm_module  # noqa: PLC0415

    real_build = arm_module.build_index

    def substituting_build(artifacts, db_path, *, reviews=()):
        tampered = artifacts.model_copy(
            update={
                "shots": tuple(
                    shot.model_copy(update={"shot_id": "sX"})
                    if shot.shot_id == "s2"
                    else shot
                    for shot in artifacts.shots
                )
            }
        )
        return real_build(tampered, db_path, reviews=reviews)

    monkeypatch.setattr(arm_module, "build_index", substituting_build)
    llm_calls: list[PassName] = []

    def counting_llm(stage: PassName, request: object) -> object:
        llm_calls.append(stage)
        return _planner_fake(stage, request)

    gemini = _VuGemini(fusion_overall=0.9)
    _deps, factory = _vu_deps(gemini)
    inputs = replace(_inputs(tmp_path), llm_call=counting_llm, video_understanding=factory)

    with pytest.raises(ArmPipelineError) as error:
        run_arm_pipeline(inputs)
    assert error.value.code == "candidate-integrity-failed"
    assert "cand-s2" in error.value.detail  # missing
    assert "cand-sX" in error.value.detail  # extra
    assert llm_calls == []
    assert gemini.events == []
    assert not (inputs.workspace / "moment-selection.json").is_file()
    assert not (inputs.workspace / "moment-selection").exists()
    assert not list((inputs.workspace / "moment-review").glob("*.json"))


def test_missing_proposal_candidate_is_refusal_before_commit(tmp_path: Path) -> None:
    """A Pass B selection that drops a discovered candidate is a typed
    ``candidate-integrity-failed`` refusal before validation and commit —
    never an implicit drop that quietly commits the rest."""

    def dropping_llm(stage: PassName, request: object) -> object:
        payload = _planner_fake(stage, request)
        if isinstance(payload, dict) and stage == "pass_b":
            payload["proposal"]["candidates"] = [
                c
                for c in payload["proposal"]["candidates"]
                if c["candidate_id"] != "cand-s3"
            ]
            payload["dimension_notes"] = [
                n
                for n in payload["dimension_notes"]
                if n["candidate_id"] != "cand-s3"
            ]
        return payload

    inputs = replace(_inputs(tmp_path), llm_call=dropping_llm)
    with pytest.raises(ArmPipelineError) as error:
        run_arm_pipeline(inputs)
    assert error.value.code == "candidate-integrity-failed"
    assert "cand-s3" in error.value.detail
    assert not (inputs.workspace / "moment-selection.json").is_file()
    assert not (inputs.workspace / "moment-selection").exists()


def test_integrity_check_reads_current_index_rows_not_cached_counts(
    tmp_path: Path,
) -> None:
    """Stale-state control: after rebuilding the index at the same path with
    an equal-count substituted fixture, the check refuses on the CURRENT
    rows (missing+extra named) — proving it evaluates exact current sets,
    not cached counts."""
    from services.cli._v44_arm_integrity import require_candidate_integrity  # noqa: PLC0415

    data = _analysis()
    artifact = build_speech_mi_artifact(
        data.episode_id, data.source_id, data.total_frames, data.speech
    )
    index_path = tmp_path / "stale-integrity.duckdb"
    build_index(artifact, index_path)
    with MediaQueryApiV2.open(index_path) as api:
        require_candidate_integrity(api, data)  # current rows match: passes

    tampered = artifact.model_copy(
        update={
            "shots": tuple(
                shot.model_copy(update={"shot_id": "sX"})
                if shot.shot_id == "s2"
                else shot
                for shot in artifact.shots
            )
        }
    )
    build_index(tampered, index_path)
    with (
        MediaQueryApiV2.open(index_path) as api,
        pytest.raises(ArmPipelineError) as error,
    ):
        require_candidate_integrity(api, data)
    assert error.value.code == "candidate-integrity-failed"
    assert "cand-s2" in error.value.detail
    assert "cand-sX" in error.value.detail


def test_compose_arm_brief_uses_operator_draft_when_present(tmp_path: Path) -> None:
    episode_root = tmp_path / "episode"
    episode_root.mkdir()
    (episode_root / "episode.json").write_text(
        json.dumps({"schema_version": "v44-episode-protocol-v1", "brief_draft": None}),
        encoding="utf-8",
    )
    neutral = compose_arm_brief(episode_root, "v44-real-01", "suda")
    assert neutral.status == "approved"
    assert neutral.approval_ref is not None
    assert neutral.approval_ref.actor_id == "operator-suda"
    assert "v44-real-01" in neutral.episode_objective

    (episode_root / "episode.json").write_text(
        json.dumps(
            {
                "schema_version": "v44-episode-protocol-v1",
                "brief_draft": "ケース探しの伏線回収エピソード",
                "title_intent": "失くしたものを探す",
            }
        ),
        encoding="utf-8",
    )
    drafted = compose_arm_brief(episode_root, "v44-real-01", "suda")
    assert drafted.episode_objective.startswith("ケース探しの伏線回収エピソード。")
    assert "ほぼ全体を残す" in drafted.episode_objective
    assert "並べ替え・凝縮" in drafted.must_not_misrepresent[1]
    assert drafted.viewer_promise == "失くしたものを探す"


def test_brief_preservation_guidance_reaches_pass_requests(tmp_path: Path) -> None:
    """Correction A contract: the guidance rides the brief into BOTH payloads.

    The llm_call input IS the model-facing payload (model_provider
    serializes exactly this request as DATA), so capturing it proves the
    operator's preservation intent reaches PassA and PassB.
    """

    captured: dict[str, object] = {}

    def capturing_llm(stage: PassName, request: object) -> object:
        captured[stage] = request
        return _planner_fake(stage, request)

    inputs = replace(_inputs(tmp_path), llm_call=capturing_llm)
    run_arm_pipeline(inputs)

    assert set(captured) >= {"pass_a", "pass_b"}
    for stage in ("pass_a", "pass_b"):
        request = captured[stage]
        assert isinstance(request, PassARequest | PassBRequest)
        assert "ほぼ全体を残す" in request.brief.episode_objective
        assert "並べ替え・凝縮" in request.brief.must_not_misrepresent[1]


def _misheard_analysis() -> ArmPipelineData:
    speech = (
        SpeechSegment(
            segment_id="s1", text="このDJIポケット4のケースが無くなった", start_frame=0,
            end_frame=90,
        ),
        SpeechSegment(
            segment_id="s2", text="PSとか値段変わるあたり", start_frame=90, end_frame=180
        ),
    )
    return ArmPipelineData(
        episode_id="v44-arm-unit",
        source_id="v44-arm-unit-edit-source",
        total_frames=180,
        speech=speech,
        transcript_segments_ms=(
            (0, 3000, "このDJIポケット4のケースが無くなった"),
            (3000, 6000, "PSとか値段変わるあたり"),
        ),
        mezzanine=None,
        mezzanine_sha256=None,
    )


def _episode_with_dictionary(tmp_path: Path, entries: object) -> Path:
    episode_root = tmp_path / "episode-nouns"
    episode_root.mkdir()
    (episode_root / "proper-nouns.json").write_text(
        json.dumps({"schema_version": "proper-nouns-ja-v1", "entries": entries}),
        encoding="utf-8",
    )
    return episode_root


def test_arm_substitutes_episode_proper_nouns_pre_mi(tmp_path: Path) -> None:
    """Correction B contract: misheard ASR terms become canonical pre-MI."""

    episode_root = _episode_with_dictionary(
        tmp_path,
        [
            {"canonical": "DJI Pocket 4", "variants": ["DJIポケット4"]},
            {"canonical": "PS5", "variants": ["PS"]},
        ],
    )
    analysis = _misheard_analysis()
    inputs = ArmPipelineInputs(
        episode_root=episode_root,
        workspace=tmp_path / "workspace",
        episode_id=analysis.episode_id,
        brief=compose_arm_brief(episode_root, analysis.episode_id, "suda"),
        llm_call=_planner_fake,
        analysis=analysis,
    )
    result = run_arm_pipeline(inputs)

    mi = _load_json(inputs.workspace / "media-intelligence.json")
    assert isinstance(mi, dict)
    descriptions = [shot["description"] for shot in mi["shots"]]
    assert "このDJI Pocket 4のケースが無くなった" in descriptions
    assert "PS5とか値段変わるあたり" in descriptions
    assert result.hypothesis_segments_ms == (
        (0, 3000, "このDJI Pocket 4のケースが無くなった"),
        (3000, 6000, "PS5とか値段変わるあたり"),
    )
    assert any(
        "proper_noun_substitutions=2" in note for note in result.notes
    ), "the substitution count must surface in the report notes"


def test_arm_substitution_without_episode_dictionary_is_noop(tmp_path: Path) -> None:
    """No proper-nouns.json → channel dictionary alone → texts unchanged."""

    episode_root = tmp_path / "episode-plain"
    episode_root.mkdir()
    analysis = _misheard_analysis()
    inputs = ArmPipelineInputs(
        episode_root=episode_root,
        workspace=tmp_path / "workspace",
        episode_id=analysis.episode_id,
        brief=compose_arm_brief(episode_root, analysis.episode_id, "suda"),
        llm_call=_planner_fake,
        analysis=analysis,
    )
    result = run_arm_pipeline(inputs)

    assert result.hypothesis_segments_ms == analysis.transcript_segments_ms
    assert any(
        "proper_noun_substitutions=0" in note for note in result.notes
    )


def test_arm_refuses_malformed_episode_dictionary(tmp_path: Path) -> None:
    """A present-but-invalid episode dictionary is a typed refusal."""

    episode_root = _episode_with_dictionary(
        tmp_path, [{"canonical": "", "variants": ["x"]}]
    )
    analysis = _misheard_analysis()
    inputs = ArmPipelineInputs(
        episode_root=episode_root,
        workspace=tmp_path / "workspace",
        episode_id=analysis.episode_id,
        brief=compose_arm_brief(episode_root, analysis.episode_id, "suda"),
        llm_call=_planner_fake,
        analysis=analysis,
    )
    with pytest.raises(ArmEvidenceError) as error:
        run_arm_pipeline(inputs)
    assert error.value.code == "proper-nouns-unreadable"


def test_compute_arm_evidence_quality_pairs_and_scores() -> None:
    corrected = TranscriptSampleV1(
        segments=(
            SampleSegment(start_ms=0, end_ms=2000, text="DJI Pocket 4 のケース"),
            SampleSegment(start_ms=2000, end_ms=4000, text="無くなったのよ"),
        ),
        proper_nouns={"DJI Pocket 4": "ディージェイアイ ポケット4"},
    )
    hypothesis = (
        (150, 2100, "DJI Pocket 4 のケース"),
        (2150, 4100, "無くなったのよ"),
    )
    quality = compute_arm_evidence_quality(corrected, hypothesis)
    assert quality.transcript_cer == 0.0
    assert quality.proper_noun_recall == 1.0
    assert quality.timestamp_error_p95_ms == 150.0
    assert quality.omitted_utterances == 0

    garbage = ((0, 1000, "全然違うはなし"),)
    missed = compute_arm_evidence_quality(corrected, garbage)
    assert missed.transcript_cer == pytest.approx(23 / 24)
    assert missed.proper_noun_recall == 0.0
    assert missed.timestamp_error_p95_ms is None


def test_dry_run_reports_toy_note_without_transport(tmp_path: Path) -> None:
    gt = EditorialGroundTruthV1(
        episode_id="v44-real-dry",
        anchors=(
            GroundTruthAnchor(anchor_id="a-1", start_frame=0, end_frame=90, label="must_keep"),
        ),
        created_at="2026-08-24T00:00:00+00:00",
        operator="suda",
    )
    gt_path = tmp_path / "gt.json"
    gt_path.write_text(gt.model_dump_json(), encoding="utf-8")
    episode_root = tmp_path / "episode"
    episode_root.mkdir()
    out = tmp_path / "report.json"
    rc = cli_main(
        [
            "run-arm",
            "--arm",
            "A",
            "--episode-root",
            str(episode_root),
            "--ground-truth",
            str(gt_path),
            "--out",
            str(out),
            "--dry-run",
        ]
    )
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert "dry-run-toy" in (report["notes"] or "")
    assert report["editorial"]["must_keep_recall"] == 1.0


def test_real_arm_refuses_heuristic_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.cli.v44_product_proof as cli  # noqa: PLC0415

    monkeypatch.setattr(cli, "_editorial_mode", lambda: "heuristic_diagnostic")
    gt = EditorialGroundTruthV1(
        episode_id="v44-real-mode",
        anchors=(
            GroundTruthAnchor(anchor_id="a-1", start_frame=0, end_frame=90, label="must_keep"),
        ),
        created_at="2026-08-24T00:00:00+00:00",
        operator="suda",
    )
    gt_path = tmp_path / "gt.json"
    gt_path.write_text(gt.model_dump_json(), encoding="utf-8")
    episode_root = tmp_path / "episode"
    episode_root.mkdir()
    rc = cli_main(
        [
            "run-arm",
            "--arm",
            "A",
            "--episode-root",
            str(episode_root),
            "--ground-truth",
            str(gt_path),
            "--out",
            str(tmp_path / "report.json"),
        ]
    )
    assert rc == 1
    assert not (tmp_path / "report.json").is_file()


def test_arm_c_on_real_episode_still_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.cli.v44_product_proof as cli  # noqa: PLC0415

    calls: list[str] = []

    def fake_gate(transport: str = "codex-exec") -> None:
        calls.append(transport)
        raise SystemExit(1)

    monkeypatch.setattr(cli, "_try_production_gate", fake_gate)
    gt = EditorialGroundTruthV1(
        episode_id="v44-real-c",
        anchors=(
            GroundTruthAnchor(anchor_id="a-1", start_frame=0, end_frame=90, label="must_keep"),
        ),
        created_at="2026-08-24T00:00:00+00:00",
        operator="suda",
    )
    gt_path = tmp_path / "gt.json"
    gt_path.write_text(gt.model_dump_json(), encoding="utf-8")
    episode_root = tmp_path / "episode"
    episode_root.mkdir()
    with pytest.raises(SystemExit):
        cli_main(
            [
                "run-arm",
                "--arm",
                "C",
                "--episode-root",
                str(episode_root),
                "--ground-truth",
                str(gt_path),
                "--out",
                str(tmp_path / "report.json"),
            ]
        )
    assert calls == ["codex-exec"]


def _binding_episode(tmp_path: Path) -> tuple[Path, Path, Path]:
    gt = EditorialGroundTruthV1(
        episode_id="v44-real-bind",
        anchors=(
            GroundTruthAnchor(anchor_id="a-1", start_frame=0, end_frame=90, label="must_keep"),
        ),
        created_at="2026-08-30T00:00:00+00:00",
        operator="suda",
    )
    gt_path = tmp_path / "gt.json"
    gt_path.write_text(gt.model_dump_json(), encoding="utf-8")
    episode_root = tmp_path / "episode"
    episode_root.mkdir()
    return gt_path, episode_root, tmp_path / "report.json"


def test_run_arm_real_refuses_absent_evaluation_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(proof, "_editorial_mode", lambda: "production_model")
    gt_path, episode_root, out = _binding_episode(tmp_path)

    rc = cli_main(
        [
            "run-arm",
            "--arm",
            "A",
            "--episode-root",
            str(episode_root),
            "--ground-truth",
            str(gt_path),
            "--out",
            str(out),
        ]
    )
    assert rc == 2
    assert not out.is_file()
    assert "evaluation-binding-missing" in capsys.readouterr().err


def test_run_arm_real_refuses_partial_evaluation_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(proof, "_editorial_mode", lambda: "production_model")
    gt_path, episode_root, out = _binding_episode(tmp_path)

    rc = cli_main(
        [
            "run-arm",
            "--arm",
            "B",
            "--episode-root",
            str(episode_root),
            "--ground-truth",
            str(gt_path),
            "--out",
            str(out),
            "--ground-truth-label",
            "v2",
        ]
    )
    assert rc == 2
    assert not out.is_file()
    err = capsys.readouterr().err
    assert "evaluation-binding-missing" in err
    assert "--evidence-lane" in err
    assert "--transcript-sha256" in err


def test_run_arm_real_refuses_malformed_binding_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(proof, "_editorial_mode", lambda: "production_model")
    gt_path, episode_root, out = _binding_episode(tmp_path)

    rc = cli_main(
        [
            "run-arm",
            "--arm",
            "A",
            "--episode-root",
            str(episode_root),
            "--ground-truth",
            str(gt_path),
            "--out",
            str(out),
            "--ground-truth-label",
            "v2",
            "--evidence-lane",
            "operator_corrected_diagnostic",
            "--transcript-sha256",
            "not-a-sha",
        ]
    )
    assert rc == 2
    assert not out.is_file()
    assert "evaluation-binding-missing" in capsys.readouterr().err


def test_run_arm_real_resolves_complete_binding_for_real_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_real_arm(  # noqa: PLR0913 (mirrors the production seam signature)
        arm: str,
        episode_root: Path,
        gt: EditorialGroundTruthV1,
        out_path: Path,
        args: argparse.Namespace,
        *,
        evaluation_binding: EvaluationBindingV1,
    ) -> int:
        captured["binding"] = evaluation_binding
        return 0

    monkeypatch.setattr(proof, "_run_real_arm", fake_real_arm)
    monkeypatch.setattr(proof, "_editorial_mode", lambda: "production_model")
    gt_path, episode_root, out = _binding_episode(tmp_path)

    rc = cli_main(
        [
            "run-arm",
            "--arm",
            "A",
            "--episode-root",
            str(episode_root),
            "--ground-truth",
            str(gt_path),
            "--out",
            str(out),
            "--ground-truth-label",
            "v2",
            "--evidence-lane",
            "operator_corrected_diagnostic",
            "--transcript-sha256",
            "e" * 64,
        ]
    )
    assert rc == 0
    binding = captured["binding"]
    assert isinstance(binding, EvaluationBindingV1)
    assert binding.ground_truth_sha256 == hashlib.sha256(gt_path.read_bytes()).hexdigest()
    assert binding.ground_truth_label == "v2"
    assert binding.evidence_lane == "operator_corrected_diagnostic"
    assert binding.transcript_sha256 == "e" * 64


def test_whisper_provider_pin_reads_sha12(tmp_path: Path) -> None:
    pins = tmp_path / "config" / "toolchains" / "pins"
    pins.mkdir(parents=True)
    (pins / "whisper-ja.json").write_text(
        json.dumps({"whisper_cli": {"sha256": "a" * 64}}), encoding="utf-8"
    )
    assert whisper_provider_pin(tmp_path) == f"whisper-cpp-cli:{'a' * 12}"

    with pytest.raises(ArmPipelineError) as error:
        whisper_provider_pin(tmp_path / "missing")
    assert error.value.code == "whisper-pin-missing"


def test_build_video_factory_returns_typed_real_deps(tmp_path: Path) -> None:
    runtime_path = _runtime_config_path()
    assert runtime_path is not None
    runtime = load_editorial_runtime(runtime_path)
    env = {
        "GEMINI_API_KEY": "fake-gemini-key",
        "GEMINI_NETWORK_ENABLED": "1",
        "ZAI_API_KEY": "fake-zai-key",
        "ZAI_NETWORK_ENABLED": "1",
    }
    workspace = tmp_path / "ws-factory"
    workspace.mkdir()
    mezzanine = tmp_path / "edit-source.mp4"
    mezzanine.write_bytes(b"\x00")
    actual_hash = hashlib.sha256(b"\x00").hexdigest()
    data = ArmPipelineData(
        episode_id="v44-arm-unit",
        source_id="v44-arm-unit-edit-source",
        total_frames=300,
        speech=_analysis().speech,
        transcript_segments_ms=_analysis().transcript_segments_ms,
        mezzanine=mezzanine,
        mezzanine_sha256=actual_hash,
    )
    artifact = build_speech_mi_artifact(
        data.episode_id, data.source_id, data.total_frames, data.speech
    )
    index_path = tmp_path / "factory-index.duckdb"
    build_index(artifact, index_path)
    with MediaQueryApiV2.open(index_path) as speech_api:
        factory = build_video_understanding_factory(
            episode_id=data.episode_id,
            workspace=workspace,
            env=env,
            runtime=runtime,
            runtime_path=runtime_path,
        )
        deps = factory(data, speech_api)
        assert isinstance(deps, VideoUnderstandingDeps)
        assert isinstance(deps.clips, ClipExtractor)
        assert deps.clips.media_path == mezzanine
        assert isinstance(deps.transcripts, RealTranscriptLookup)
        assert isinstance(deps.audio, RealAudioContext)
        assert deps.gemini.pin.model_id == "gemini-3.7-flash"
        assert (
            deps.gemini.pin.endpoint
            == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent"
        )
        assert deps.specialist.pin.model_id == "glm-5v-turbo"
        assert deps.specialist.pin.endpoint == "https://api.z.ai/api/coding/paas/v4/chat/completions"


def test_build_video_factory_missing_mezzanine_is_typed_refusal_before_transport(
    tmp_path: Path,
) -> None:

    runtime_path = _runtime_config_path()
    assert runtime_path is not None
    runtime = load_editorial_runtime(runtime_path)
    env = {
        "GEMINI_API_KEY": "fake-gemini-key",
        "GEMINI_NETWORK_ENABLED": "1",
        "ZAI_API_KEY": "fake-zai-key",
        "ZAI_NETWORK_ENABLED": "1",
    }
    workspace = tmp_path / "ws-missing"
    workspace.mkdir()
    data = ArmPipelineData(
        episode_id="v44-arm-unit",
        source_id="v44-arm-unit-edit-source",
        total_frames=300,
        speech=_analysis().speech,
        transcript_segments_ms=_analysis().transcript_segments_ms,
        mezzanine=None,
        mezzanine_sha256=None,
    )
    artifact = build_speech_mi_artifact(
        data.episode_id, data.source_id, data.total_frames, data.speech
    )
    index_path = tmp_path / "missing-index.duckdb"
    build_index(artifact, index_path)
    with MediaQueryApiV2.open(index_path) as speech_api:
        factory = build_video_understanding_factory(
            episode_id=data.episode_id,
            workspace=workspace,
            env=env,
            runtime=runtime,
            runtime_path=runtime_path,
        )
        with pytest.raises(ArmPipelineError) as error:
            factory(data, speech_api)
        assert error.value.code == "video-understanding-missing-mezzanine"
        assert "video-understanding-missing-mezzanine" in str(error.value)
        assert "fake-gemini-key" not in str(error.value)
        assert "fake-zai-key" not in str(error.value)


def test_factory_resolves_pins_from_runtime_base_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:


    runtime_path = _runtime_config_path()
    assert runtime_path is not None
    runtime = load_editorial_runtime(runtime_path)
    env = {
        "GEMINI_API_KEY": "fake-gemini-key",
        "GEMINI_NETWORK_ENABLED": "1",
        "ZAI_API_KEY": "fake-zai-key",
        "ZAI_NETWORK_ENABLED": "1",
    }
    workspace = tmp_path / "ws-cwd"
    workspace.mkdir()
    orig_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        factory = build_video_understanding_factory(
            episode_id="v44-arm-unit",
            workspace=workspace,
            env=env,
            runtime=runtime,
            runtime_path=runtime_path,
        )
        assert callable(factory)
    finally:
        os.chdir(orig_cwd)


def test_factory_hash_mismatch_is_typed_refusal_before_transport(tmp_path: Path) -> None:

    runtime_path = _runtime_config_path()
    assert runtime_path is not None
    runtime = load_editorial_runtime(runtime_path)
    env = {
        "GEMINI_API_KEY": "fake-gemini-key",
        "GEMINI_NETWORK_ENABLED": "1",
        "ZAI_API_KEY": "fake-zai-key",
        "ZAI_NETWORK_ENABLED": "1",
    }
    workspace = tmp_path / "ws-hash"
    workspace.mkdir()
    mezzanine = tmp_path / "edit-source-hash.mp4"
    mezzanine.write_bytes(b"real-bytes")
    wrong_hash = "b" * 64
    data = ArmPipelineData(
        episode_id="v44-arm-unit",
        source_id="v44-arm-unit-edit-source",
        total_frames=300,
        speech=_analysis().speech,
        transcript_segments_ms=_analysis().transcript_segments_ms,
        mezzanine=mezzanine,
        mezzanine_sha256=wrong_hash,
    )
    artifact = build_speech_mi_artifact(
        data.episode_id, data.source_id, data.total_frames, data.speech
    )
    index_path = tmp_path / "hash-index.duckdb"
    build_index(artifact, index_path)
    with MediaQueryApiV2.open(index_path) as speech_api:
        factory = build_video_understanding_factory(
            episode_id=data.episode_id,
            workspace=workspace,
            env=env,
            runtime=runtime,
            runtime_path=runtime_path,
        )
        with pytest.raises(ArmPipelineError) as error:
            factory(data, speech_api)
        assert "hash" in error.value.code
        assert wrong_hash not in str(error.value)
        assert "real-bytes" not in str(error.value)
        assert "fake-gemini-key" not in str(error.value)


def test_video_transport_rejects_alternate_path_before_socket() -> None:
    transport = make_video_http_post()
    with pytest.raises(ValueError, match="endpoint-not-pinned") as error:
        transport("https://api.z.ai/evil", {}, b"{}", 5.0)
    assert "endpoint-not-pinned" in str(error.value)
    assert "fake" not in str(error.value)


def test_video_transport_rejects_non_default_port_before_socket() -> None:
    transport = make_video_http_post()
    with pytest.raises(ValueError, match="endpoint-not-pinned") as error:
        transport("https://api.z.ai:8443/api/coding/paas/v4/chat/completions", {}, b"{}", 5.0)
    assert "endpoint-not-pinned" in str(error.value)


def test_cli_arm_a_never_builds_video_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    called: list[str] = []

    def fake_factory(
        *, episode_id: str, workspace: Path, env: dict[str, str], runtime, runtime_path: Path
    ) -> object:
        called.append("factory")
        raise AssertionError("Arm A must never build a video factory")

    monkeypatch.setattr(
        "services.cli._v44_arm_video_factory.build_video_understanding_factory", fake_factory
    )
    monkeypatch.setattr(
        "services.cli.live_editorial_codex.make_codex_runner", lambda: (lambda *a, **k: "fake")
    )
    monkeypatch.setattr(proof, "_editorial_mode", lambda: "production_model")
    monkeypatch.setattr(proof, "_try_production_gate", lambda transport="codex-exec": None)
    monkeypatch.setattr(
        "services.cli.v44_arm_pipeline.run_arm_pipeline",
        lambda inputs: ArmPipelineResult(
            kept_spans_mezz=((0, 90),),
            kept_candidate_ids=("cand-1",),
            escalated_candidate_ids=(),
            escalated_spans_mezz=(),
            reviews=(),
            commit_version=2,
            proposal_id="test",
            candidate_count=1,
            wall_seconds=1.0,
            hypothesis_segments_ms=(),
            notes=(),
        ),
    )

    gt = EditorialGroundTruthV1(
        episode_id="v44-real-01",
        anchors=(
            GroundTruthAnchor(anchor_id="a-1", start_frame=0, end_frame=90, label="must_keep"),
        ),
        created_at="2026-08-24T00:00:00+00:00",
        operator="suda",
    )
    gt_path = tmp_path / "gt.json"
    gt_path.write_text(gt.model_dump_json(), encoding="utf-8")
    episode_root = tmp_path / "episode-real-a"
    episode_root.mkdir()
    (episode_root / "sources").mkdir()
    monkeypatch.setenv("EDITORIAL_DIRECTOR_API_KEY", "fake-director-key")
    monkeypatch.setenv("EDITORIAL_DIRECTOR_NETWORK_ENABLED", "1")
    out = tmp_path / "out.json"
    rc = proof._run_real_arm(
        "A",
        episode_root,
        gt,
        out,
        argparse.Namespace(workspace=str(tmp_path / "ws-a")),
        evaluation_binding=_eval_binding(),
    )
    assert called == []
    assert rc == 0
    captured = capsys.readouterr()
    assert "video-understanding-unwired" not in captured.err


def test_cli_arm_b_passes_concrete_factory_to_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_pipeline(inputs: ArmPipelineInputs) -> object:
        captured["factory"] = inputs.video_understanding
        assert inputs.video_understanding is not None
        mezzanine = tmp_path / "cli-mezzanine.mp4"
        mezzanine.write_bytes(b"\x00")
        actual_hash = hashlib.sha256(b"\x00").hexdigest()
        data = ArmPipelineData(
            episode_id="v44-arm-unit",
            source_id="v44-arm-unit-edit-source",
            total_frames=300,
            speech=_analysis().speech,
            transcript_segments_ms=_analysis().transcript_segments_ms,
            mezzanine=mezzanine,
            mezzanine_sha256=actual_hash,
        )
        artifact = build_speech_mi_artifact(
            data.episode_id, data.source_id, data.total_frames, data.speech
        )
        idx = tmp_path / "cli-factory-index.duckdb"
        build_index(artifact, idx)
        with MediaQueryApiV2.open(idx) as api:
            factory = inputs.video_understanding
            assert factory is not None
            deps = factory(data, api)
            assert isinstance(deps, VideoUnderstandingDeps)
            assert isinstance(deps.clips, ClipExtractor)
            assert isinstance(deps.transcripts, RealTranscriptLookup)
            assert isinstance(deps.audio, RealAudioContext)
        return run_arm_pipeline(_inputs(tmp_path))

    monkeypatch.setattr("services.cli.v44_arm_pipeline.run_arm_pipeline", fake_run_pipeline)
    monkeypatch.setattr(
        "services.cli.live_editorial_codex.make_codex_runner", lambda: (lambda *a, **k: "fake")
    )
    monkeypatch.setattr(proof, "_editorial_mode", lambda: "production_model")
    monkeypatch.setattr(proof, "_try_production_gate", lambda transport="codex-exec": None)

    gt = EditorialGroundTruthV1(
        episode_id="v44-real-01",
        anchors=(
            GroundTruthAnchor(anchor_id="a-1", start_frame=0, end_frame=90, label="must_keep"),
        ),
        created_at="2026-08-24T00:00:00+00:00",
        operator="suda",
    )
    gt_path = tmp_path / "gt.json"
    gt_path.write_text(gt.model_dump_json(), encoding="utf-8")
    episode_root = tmp_path / "episode-real"
    episode_root.mkdir()
    (episode_root / "sources").mkdir()
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    monkeypatch.setenv("GEMINI_NETWORK_ENABLED", "1")
    monkeypatch.setenv("ZAI_API_KEY", "fake-zai-key")
    monkeypatch.setenv("ZAI_NETWORK_ENABLED", "1")
    monkeypatch.setenv("EDITORIAL_DIRECTOR_API_KEY", "fake-director-key")
    monkeypatch.setenv("EDITORIAL_DIRECTOR_NETWORK_ENABLED", "1")

    out = tmp_path / "out.json"
    rc = proof._run_real_arm(
        "B",
        episode_root,
        gt,
        out,
        argparse.Namespace(workspace=str(tmp_path / "ws")),
        evaluation_binding=_eval_binding(),
    )
    assert rc == 0
    assert captured["factory"] is not None


def test_cli_arm_b_missing_credentials_is_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "services.cli.live_editorial_codex.make_codex_runner",
        lambda: (lambda *a, **k: "fake"),
    )
    monkeypatch.setattr(proof, "_editorial_mode", lambda: "production_model")
    monkeypatch.setattr(proof, "_try_production_gate", lambda transport="codex-exec": None)

    gt = EditorialGroundTruthV1(
        episode_id="v44-real-01",
        anchors=(
            GroundTruthAnchor(anchor_id="a-1", start_frame=0, end_frame=90, label="must_keep"),
        ),
        created_at="2026-08-24T00:00:00+00:00",
        operator="suda",
    )
    gt_path = tmp_path / "gt.json"
    gt_path.write_text(gt.model_dump_json(), encoding="utf-8")
    episode_root = tmp_path / "episode-real"
    episode_root.mkdir()
    (episode_root / "sources").mkdir()
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_NETWORK_ENABLED", raising=False)
    monkeypatch.delenv("ZAI_NETWORK_ENABLED", raising=False)
    monkeypatch.setenv("EDITORIAL_DIRECTOR_API_KEY", "fake-director-key")
    monkeypatch.setenv("EDITORIAL_DIRECTOR_NETWORK_ENABLED", "1")

    out = tmp_path / "out.json"
    rc = proof._run_real_arm(
        "B",
        episode_root,
        gt,
        out,
        argparse.Namespace(workspace=str(tmp_path / "ws")),
        evaluation_binding=_eval_binding(),
    )
    assert rc == 1
    assert not out.is_file()
    err = capsys.readouterr().err
    assert "production-model-unavailable" in err
    assert "GEMINI_API_KEY" in err or "ZAI_API_KEY" in err
    assert "video-understanding-unwired" not in err
    assert "fake-gemini-key" not in err
    assert "fake-zai-key" not in err
