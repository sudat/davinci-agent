# allow: SIZE_OK — the v44 T6 plan names one focused contract-test module for the
# orchestration seam; neighboring media-intelligence test modules run longer
# (test_video_review_providers.py 700+, test_moment_review_real.py 576) and
# splitting the contract would scatter one orchestration surface across files.
"""T6-repair video-understanding orchestration (map → reduce → targeted GLM →
fusion) — Tier A.

Deterministic and network-free at the orchestration seam: providers are fakes
returning STAGE-SPECIFIC typed payloads with call traces (the concrete T4
adapters keep their own live-proof module; the trace/attempts contracts are
pinned there too). Proves the six repaired defects: (1) hard specialist
targets are globally reserved before any advisory selection; (2) the episode
reduce's OWN typed specialist requests — never caller input — join the
selection and are validated inside the source; (3) every committed fused
review carries local → global_reduce → overlapping specialists → fusion in
stage lineage, with the reduce stage spanning the validated full episode;
(4) stage lineage records the ACTUAL transport attempt count (2 after a
malformed-then-valid adapter retry, 1 for transport-shape failures);
(5) fusion output is a strict typed judgment (sub-span, handles, rationale
candidates, confidence) — no hardcoded 0.6/0.3, no middle-third; (6) an
advisory gap the fusion does not explicitly acknowledge blocks typed.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from services.contracts.primitives import RationalFrameRate
from services.editorial_v2.editorial_pins import (
    MOMENT_REVIEW_PIN_PATH,
    MOMENT_REVIEW_SPECIALIST_PIN_PATH,
    EditorialPinV2,
    load_editorial_pin,
)
from services.foundation_io import canonical_model_bytes
from services.media_intelligence import video_understanding as vu_module
from services.media_intelligence.budget import (
    BudgetExpansionUnjustifiedError,
    BudgetLeadMapCoverageError,
    DeepReviewWindow,
)
from services.media_intelligence.lead_map import LeadMapWindowPolicy
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
    MomentDeepReviewV1,
    ReviewStageLineage,
    ReviewWindow,
    SyntheticAudioContext,
    SyntheticTranscriptLookup,
    TranscriptRef,
    derive_moment_review_id,
    review_rows,
)
from services.media_intelligence.moment_review_real import require_real_lineage
from services.media_intelligence.video_clip_evidence import VideoClipEvidence
from services.media_intelligence.video_clip_extraction import ClipEvidencePair
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
    SpecialistRequestSpan,
    UnresolvedRange,
)
from services.media_intelligence.video_understanding import (
    VIDEO_UNDERSTANDING_TOOL,
    HardSpecialistTargetError,
    StageRangeMismatchError,
    VideoUnderstandingDeps,
    VideoUnderstandingError,
    VideoUnderstandingRequest,
    VideoUnderstandingResult,
    run_video_understanding,
    validate_local_coverage,
)
from services.media_intelligence.video_understanding_targets import (
    SpecialistSelectionError,
    select_specialist_targets,
)
from services.media_query import v2_models as vm
from services.media_query.index_v2 import build_index
from services.media_query.query_v2 import MediaQueryApiV2

EPISODE_ID = "v44-t6"
TOTAL_FRAMES = 100
RATE: Final = RationalFrameRate(num=30, den=1)
LEAD_POLICY: Final = LeadMapWindowPolicy(max_window_frames=40, overlap_frames=10,
                                          snap_radius_frames=5)
LOCAL_BOUNDS: Final = ((0, 40), (30, 70), (60, 100))
HARD_WINDOW: Final = DeepReviewWindow(start_frame=10, end_frame=20,
                                      trigger_reason="human_request",
                                      trigger_source="shot-hard")
ADVISORY_WINDOW: Final = DeepReviewWindow(start_frame=50, end_frame=55,
                                          trigger_reason="uncertain",
                                          trigger_source="shot-a")
GAP_ACK: Final[tuple[tuple[int, int], ...]] = ((50, 55),)


# ------------------------------------------------------------ fakes at the seams


def _gemini_review(bounds: tuple[int, int]) -> GeminiClipReview:
    start, end = bounds
    return GeminiClipReview(
        analyzed_start_frame=start,
        analyzed_end_frame=end,
        summary=f"local summary for [{start}, {end})",
        observations=(f"observation-{start}",),
        audio_note=f"audio note {start}",
    )


def _reduce_result(
    *,
    requests: tuple[SpecialistRequestSpan, ...] = (),
    analyzed: tuple[int, int] = (0, TOTAL_FRAMES),
) -> GeminiEpisodeReduce:
    return GeminiEpisodeReduce(
        analyzed_start_frame=analyzed[0],
        analyzed_end_frame=analyzed[1],
        summary="reduce summary of every local window",
        observations=("reduce observation",),
        audio_note="reduce audio note",
        specialist_requests=requests,
    )


def _fusion_review(
    bounds: tuple[int, int],
    *,
    acks: tuple[tuple[int, int], ...] = (),
    sub_span: tuple[int, int] | None = None,
    analyzed: tuple[int, int] | None = None,
) -> GeminiFusionReview:
    start, end = bounds
    chosen = sub_span or (start + 5, end - 5)
    return GeminiFusionReview(
        analyzed_start_frame=(analyzed or bounds)[0],
        analyzed_end_frame=(analyzed or bounds)[1],
        subject_action_evolution="typed subject action evolution",
        reaction_notes="typed reaction notes",
        timing_notes="typed timing notes",
        best_sub_span=FusionSubSpan(start_frame=chosen[0], end_frame=chosen[1]),
        keep_rationale_candidates=("typed keep candidate",),
        remove_rationale_candidates=(),
        cut_in_handle="typed cut-in handle",
        cut_out_handle="typed cut-out handle",
        confidence=FusionConfidence(
            overall=0.9, subject_action_evolution=0.8, reaction_notes=0.7,
            timing_notes=0.6, best_sub_span=0.5,
        ),
        unresolved_acknowledgements=tuple(
            UnresolvedRange(start_frame=s, end_frame=e) for s, e in acks
        ),
    )


def _glm_observation(bounds: tuple[int, int]) -> GlmClipObservation:
    start, end = bounds
    return GlmClipObservation(
        analyzed_start_frame=start,
        analyzed_end_frame=end,
        visual_findings=(f"visual finding {start}",),
        uncertainty="small text legibility",
    )


def _trace[ModelT](payload: ModelT, attempts: int = 1) -> WireOutcome[ModelT]:
    return WireOutcome(payload=payload, attempts=attempts)


@dataclass(frozen=True, slots=True)
class _GeminiScript:
    """Per-fake provider scripting (Smell-2 grouping for test wiring)."""

    local: dict[tuple[int, int], GeminiClipReview | BaseException] = field(
        default_factory=dict
    )
    local_attempts: int = 1
    reduce_result: GeminiEpisodeReduce | BaseException | None = None
    reduce_attempts: int = 1
    fusion_failures: dict[tuple[int, int], BaseException] = field(default_factory=dict)
    fusion_attempts: int = 1
    fusion_acks: tuple[tuple[int, int], ...] = ()
    fusion_transform: Callable[[GeminiFusionReview], GeminiFusionReview] | None = None


class _FakeGemini:
    """Structural stand-in for ``GeminiVideoReviewProvider`` (trace seam)."""

    def __init__(self, pin: EditorialPinV2, script: _GeminiScript | None = None) -> None:
        self.pin = pin
        self._script = script or _GeminiScript()
        self.calls: list[tuple[str, tuple[int, int], str]] = []

    def review_with_trace(
        self, purpose: str, clip: VideoClipEvidence, untrusted_data: str
    ) -> WireOutcome[GeminiStageResult]:
        bounds = (clip.requested_range.start_frame, clip.requested_range.end_frame)
        self.calls.append((purpose, bounds, untrusted_data))
        script = self._script
        if purpose == "local_map":
            scripted = script.local.get(bounds)
            if isinstance(scripted, BaseException):
                raise scripted
            payload = scripted if scripted is not None else _gemini_review(bounds)
            return _trace(payload, script.local_attempts)
        if purpose == "global_reduce":
            if isinstance(script.reduce_result, BaseException):
                raise script.reduce_result
            return _trace(script.reduce_result or _reduce_result(), script.reduce_attempts)
        failure = script.fusion_failures.get(bounds)
        if failure is not None:
            raise failure
        fusion = _fusion_review(bounds, acks=script.fusion_acks)
        if script.fusion_transform is not None:
            fusion = script.fusion_transform(fusion)
        return _trace(fusion, script.fusion_attempts)


class _FakeSpecialist:
    """Structural stand-in for ``GlmVisualSpecialistProvider`` (trace seam)."""

    def __init__(
        self,
        pin: EditorialPinV2,
        *,
        fail: frozenset[tuple[int, int]] = frozenset(),
        mismatch: tuple[int, int] | None = None,
        attempts: int = 1,
    ) -> None:
        self.pin = pin
        self._fail = fail
        self._mismatch = mismatch
        self._attempts = attempts
        self.calls: list[tuple[tuple[int, int], str]] = []

    def observe_with_trace(
        self, clip: VideoClipEvidence, untrusted_data: str
    ) -> WireOutcome[GlmClipObservation]:
        bounds = (clip.requested_range.start_frame, clip.requested_range.end_frame)
        self.calls.append((bounds, untrusted_data))
        if bounds in self._fail:
            raise VideoProviderError("provider-transport", "stable fake transport failure",
                                     attempts=self._attempts)
        if bounds == self._mismatch:
            return _trace(_glm_observation((bounds[0], bounds[1] + 1)), self._attempts)
        return _trace(_glm_observation(bounds), self._attempts)


@dataclass(slots=True)
class _FakeClips:
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
        window = ReviewWindow(start_frame=start, end_frame=end)
        digest = hashlib.sha256(f"t6-fake-clip-{start}-{end}".encode()).hexdigest()
        return VideoClipEvidence(
            ref=f"file:///tmp/t6-fake/clip-{start:06d}-{end:06d}.mp4",
            sha256=digest,
            requested_range=window,
            analyzed_range=window,
            audio_present=audio_present,
            duration_seconds=(end - start) / 30.0,
        )


def _scripted_gemini(script: _GeminiScript) -> _FakeGemini:
    return _FakeGemini(load_editorial_pin(MOMENT_REVIEW_PIN_PATH), script)


def _request(**overrides: object) -> VideoUnderstandingRequest:
    values: dict[str, object] = {
        "episode_id": EPISODE_ID,
        "source_duration_frames": TOTAL_FRAMES,
        "progressive_windows": (HARD_WINDOW, ADVISORY_WINDOW),
        "speech_boundaries": (),
        "lead_policy": LEAD_POLICY,
    }
    values.update(overrides)
    return VideoUnderstandingRequest.model_validate(values)


def _deps(
    gemini: _FakeGemini | None = None,
    specialist: _FakeSpecialist | None = None,
) -> tuple[VideoUnderstandingDeps, _FakeGemini, _FakeSpecialist]:
    fake_gemini = gemini or _scripted_gemini(_GeminiScript())
    fake_specialist = specialist or _FakeSpecialist(
        load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH)
    )
    deps = VideoUnderstandingDeps(
        gemini=fake_gemini,
        specialist=fake_specialist,
        clips=_FakeClips(),
        transcripts=SyntheticTranscriptLookup(
            segments=(TranscriptRef(segment_id="tr-1", start_frame=20, end_frame=45),)
        ),
        audio=SyntheticAudioContext(),
    )
    return deps, fake_gemini, fake_specialist


def _run(
    *,
    gemini: _FakeGemini | None = None,
    specialist: _FakeSpecialist | None = None,
    **overrides: object,
) -> tuple[VideoUnderstandingResult, _FakeGemini, _FakeSpecialist]:
    deps, fake_gemini, fake_specialist = _deps(gemini=gemini, specialist=specialist)
    result = run_video_understanding(_request(**overrides), deps)
    return result, fake_gemini, fake_specialist


def _purposes(review: MomentDeepReviewV1) -> tuple[str, ...]:
    return tuple(stage.purpose for stage in review.lineage.stage_lineage)


def _stages(review: MomentDeepReviewV1, purpose: str) -> tuple[ReviewStageLineage, ...]:
    return tuple(s for s in review.lineage.stage_lineage if s.purpose == purpose)


# ------------------------------------------------------------ happy path


def test_full_flow_commits_one_fused_review_per_local_window() -> None:
    result, gemini, specialist = _run()
    reviews = result.reviews

    assert tuple((r.source_window.start_frame, r.source_window.end_frame)
                 for r in reviews) == LOCAL_BOUNDS
    assert all(
        r.review_id == derive_moment_review_id(EPISODE_ID, s, e)
        for r, (s, e) in zip(reviews, LOCAL_BOUNDS, strict=True)
    )
    pin = load_editorial_pin(MOMENT_REVIEW_PIN_PATH)
    for review in reviews:
        assert review.lineage.provider == pin.api_surface
        assert review.lineage.provider_version == pin.model_id
        assert review.lineage.tool == VIDEO_UNDERSTANDING_TOOL

    purposes = [call[0] for call in gemini.calls]
    assert purposes.count("local_map") == 3
    assert purposes.count("global_reduce") == 1
    assert purposes.count("fusion") == 3
    assert len(specialist.calls) == 2
    assert result.lead_map_budget.budget_kind == "lead_map"
    assert result.targeted_budget.budget_kind == "targeted_deep_review"
    assert result.deferred == ()
    assert result.reduce_stage.purpose == "global_reduce"


def test_stage_lineage_orders_local_reduce_specialists_fusion() -> None:
    """Every committed fused review carries exactly one local stage, THE one
    global-reduce stage, zero-or-more overlapping specialist stages, then
    exactly one fusion stage — reduce provenance lives in the artifact."""

    result, _gemini, _specialist = _run()
    for index, review in enumerate(result.reviews):
        expected = ("local_map", "global_reduce")
        if index in (0, 1):  # hard [10, 20) ⊂ [0, 40); advisory [50, 55) ⊂ [30, 70)
            expected += ("specialist",)
        expected += ("fusion",)
        assert _purposes(review) == expected


def test_reduce_stage_spans_the_full_validated_episode() -> None:
    """The reduce lineage's requested/analyzed span is the episode-level
    [0, total) union, not the first local window's transport clip bounds."""

    result, _gemini, _specialist = _run()
    reduce_stage = result.reduce_stage
    assert (reduce_stage.requested_start_frame, reduce_stage.requested_end_frame) == (0, 100)
    assert (reduce_stage.analyzed_start_frame, reduce_stage.analyzed_end_frame) == (0, 100)
    for review in result.reviews:
        stage = _stages(review, "global_reduce")[0]
        assert (stage.requested_start_frame, stage.requested_end_frame) == (0, 100)


def test_fusion_judgments_come_from_the_typed_payload() -> None:
    """Assessment fields, the best sub-span, cut handles, and every
    confidence value are the fusion DTO's typed judgments — no constants,
    no middle-third geometry."""

    result, _gemini, _specialist = _run()
    review = result.reviews[0]
    assessment = review.assessment
    assert assessment.subject_action_evolution == "typed subject action evolution"
    assert assessment.reaction_notes == "typed reaction notes"
    assert assessment.timing_notes == "typed timing notes"
    assert assessment.best_sub_span.start_frame == 5
    assert assessment.best_sub_span.end_frame == 35  # NOT the middle third (13, 27)
    assert assessment.cut_in_handle == "typed cut-in handle"
    assert assessment.cut_out_handle == "typed cut-out handle"
    assert review.confidence.overall == 0.9
    assert review.confidence.subject_action_evolution == 0.8
    assert review.confidence.reaction_notes == 0.7
    assert review.confidence.timing_notes == 0.6
    assert review.confidence.best_sub_span == 0.5


def test_deterministic_rerun_yields_identical_review_ids_and_content() -> None:
    first, _g1, _s1 = _run()
    second, _g2, _s2 = _run()
    assert [r.review_id for r in first.reviews] == [r.review_id for r in second.reviews]
    assert [canonical_model_bytes(r) for r in first.reviews] == [
        canonical_model_bytes(r) for r in second.reviews
    ]
    assert first.reduce_stage == second.reduce_stage


def test_stage_lineage_records_the_full_contract() -> None:
    result, _gemini, specialist = _run()
    review = result.reviews[0]
    stage = _stages(review, "local_map")[0]
    pin = load_editorial_pin(MOMENT_REVIEW_PIN_PATH)

    assert stage.provider == pin.api_surface
    assert stage.model_id == pin.model_id
    assert stage.pin_sha256 == hashlib.sha256(canonical_model_bytes(pin)).hexdigest()
    assert stage.tool == VIDEO_UNDERSTANDING_TOOL
    assert (stage.requested_start_frame, stage.requested_end_frame) == (0, 40)
    assert (stage.analyzed_start_frame, stage.analyzed_end_frame) == (0, 40)
    assert re.fullmatch(r"[0-9a-f]{64}", stage.input_sha256)
    assert re.fullmatch(r"[0-9a-f]{64}", stage.output_sha256 or "")
    assert stage.attempts >= 1
    assert stage.outcome == "analyzed"

    glm_stage = _stages(result.reviews[0], "specialist")[0]
    glm_pin = load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH)
    assert glm_stage.provider == glm_pin.api_surface
    assert glm_stage.pin_sha256 == hashlib.sha256(canonical_model_bytes(glm_pin)).hexdigest()
    assert specialist.calls[0][0] == (10, 20)


# ------------------------------------------------------------ attempts contract


def test_stage_lineage_records_actual_adapter_retry_count() -> None:
    """A malformed-first-valid-second adapter retry must surface as
    attempts=2 in lineage; one-shot stages record attempts=1."""

    gemini = _scripted_gemini(_GeminiScript(local_attempts=2, reduce_attempts=2,
                                   fusion_attempts=2))
    specialist = _FakeSpecialist(load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH),
                                 attempts=2)
    result, _gemini, _specialist = _run(gemini=gemini, specialist=specialist)

    assert _stages(result.reviews[0], "local_map")[0].attempts == 2
    assert result.reduce_stage.attempts == 2
    assert _stages(result.reviews[0], "specialist")[0].attempts == 2
    assert _stages(result.reviews[0], "fusion")[0].attempts == 2


def test_transport_shaped_specialist_failure_records_single_attempt() -> None:
    specialist = _FakeSpecialist(
        load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH),
        fail=frozenset({(50, 55)}),
        attempts=1,
    )
    result, _gemini, _specialist = _run(
        gemini=_scripted_gemini(_GeminiScript(fusion_acks=GAP_ACK)), specialist=specialist
    )
    failed = [s for s in result.specialist_stages if s.outcome == "failed"]
    assert len(failed) == 1
    assert failed[0].attempts == 1


# ------------------------------------------------------------ coverage contracts


def test_local_analyzed_range_mismatch_blocks_the_run() -> None:
    scripted = GeminiClipReview.model_validate(
        {**_gemini_review((0, 40)).model_dump(), "analyzed_start_frame": 1}
    )
    gemini = _scripted_gemini(_GeminiScript(local={(0, 40): scripted}))
    deps, fake_gemini, fake_specialist = _deps(gemini=gemini)

    with pytest.raises(StageRangeMismatchError) as error:
        run_video_understanding(_request(), deps)
    assert "local_map" in error.value.detail or "[0, 40)" in error.value.detail
    assert [c[0] for c in fake_gemini.calls] == ["local_map"]
    assert fake_specialist.calls == []


def test_local_provider_failure_blocks_before_any_specialist_or_fusion() -> None:
    failure = VideoProviderError("provider-http-status", "the endpoint answered HTTP 429")
    gemini = _scripted_gemini(_GeminiScript(local={(0, 40): failure}))
    deps, fake_gemini, fake_specialist = _deps(gemini=gemini)

    with pytest.raises(VideoProviderError):
        run_video_understanding(_request(), deps)
    assert [c[0] for c in fake_gemini.calls] == ["local_map"]
    assert fake_specialist.calls == []


def test_reduce_failure_blocks_the_run() -> None:
    gemini = _scripted_gemini(_GeminiScript(
        reduce_result=VideoProviderError("provider-timeout", "exceeded 120s")))
    deps, fake_gemini, fake_specialist = _deps(gemini=gemini)

    with pytest.raises(VideoProviderError):
        run_video_understanding(_request(), deps)
    purposes = [c[0] for c in fake_gemini.calls]
    assert purposes == ["local_map"] * 3 + ["global_reduce"]
    assert fake_specialist.calls == []


def test_fusion_failure_blocks_and_commits_nothing() -> None:
    gemini = _scripted_gemini(_GeminiScript(fusion_failures={
        (0, 40): VideoProviderError("provider-response-empty", "empty body")}))
    deps, fake_gemini, _fake_specialist = _deps(gemini=gemini)

    with pytest.raises(VideoProviderError):
        run_video_understanding(_request(), deps)
    purposes = [c[0] for c in fake_gemini.calls]
    assert purposes.count("fusion") == 1


def test_fusion_analyzed_range_mismatch_blocks_the_run() -> None:
    def shifted(fusion: GeminiFusionReview) -> GeminiFusionReview:
        return fusion.model_copy(
            update={"analyzed_end_frame": fusion.analyzed_end_frame + 1})

    gemini = _scripted_gemini(_GeminiScript(fusion_transform=shifted))
    deps, _fake_gemini, _fake_specialist = _deps(gemini=gemini)
    with pytest.raises(StageRangeMismatchError):
        run_video_understanding(_request(), deps)


def test_local_coverage_union_gap_is_typed_rejection() -> None:
    budget = validate_local_coverage(TOTAL_FRAMES, ((0, 40), (40, 100)), RATE)
    assert budget.budget_kind == "lead_map"
    assert budget.frame_or_token_counters.frames == TOTAL_FRAMES

    with pytest.raises(BudgetLeadMapCoverageError):
        validate_local_coverage(TOTAL_FRAMES, ((0, 40), (50, 100)), RATE)
    with pytest.raises(BudgetLeadMapCoverageError):
        validate_local_coverage(TOTAL_FRAMES, ((0, 40), (0, 40), (50, 100)), RATE)


# ------------------------------------------------------------ reduce-driven targets


def test_reduce_output_supplies_validated_specialist_targets() -> None:
    """The reduce typed result itself carries specialist target requests;
    they are validated inside the source, joined with the deterministic
    progressive windows, and enforced under the 25% targeted budget."""

    requests = (SpecialistRequestSpan(start_frame=80, end_frame=90,
                                      rationale="dense on-screen text region",
                                      uncertainty=None),)
    result, _gemini, specialist = _run(
        gemini=_scripted_gemini(_GeminiScript(reduce_result=_reduce_result(requests=requests)))
    )

    called = [bounds for bounds, _doc in specialist.calls]
    assert (80, 90) in called
    assert (10, 20) in called
    assert (50, 55) in called
    selected = [(w.start_frame, w.end_frame) for w in result.targeted_budget.deep_review_windows]
    assert (80, 90) in selected
    fused_last = next(r for r in result.reviews if r.source_window.start_frame == 60)
    assert len(_stages(fused_last, "specialist")) == 1


def test_reduce_target_outside_source_is_typed_rejection() -> None:
    requests = (SpecialistRequestSpan(start_frame=90, end_frame=120,
                                      rationale="beyond the episode", uncertainty=None),)
    with pytest.raises(SpecialistSelectionError):
        _run(gemini=_scripted_gemini(_GeminiScript(
            reduce_result=_reduce_result(requests=requests))))
    empty = (SpecialistRequestSpan(start_frame=30, end_frame=30, rationale="empty",
                                   uncertainty=None),)
    with pytest.raises(SpecialistSelectionError):
        _run(gemini=_scripted_gemini(_GeminiScript(
            reduce_result=_reduce_result(requests=empty))))


def test_reduce_bounds_must_cover_the_full_episode() -> None:
    """A reduce that reports only the anchor clip bounds has NOT covered the
    episode: the run blocks typed before any GLM or fusion call — full-source
    coverage is provider-reported and validated, never manufactured."""

    gemini = _scripted_gemini(_GeminiScript(reduce_result=_reduce_result(
        analyzed=(0, 40))))  # the first local window's transport-clip bounds
    deps, fake_gemini, fake_specialist = _deps(gemini=gemini)
    with pytest.raises(StageRangeMismatchError) as error:
        run_video_understanding(_request(), deps)
    assert "global_reduce" in error.value.detail or "[0, 40)" in error.value.detail
    assert [c[0] for c in fake_gemini.calls] == ["local_map"] * 3 + ["global_reduce"]
    assert fake_specialist.calls == []


def test_reduce_rationale_survives_selection_into_the_glm_document() -> None:
    """The reduce's per-target rationale and uncertainty must reach the GLM
    specialist's untrusted input document for that exact range; deterministic
    progressive targets carry no provider rationale."""

    requests = (SpecialistRequestSpan(start_frame=80, end_frame=90,
                                      rationale="dense on-screen text region",
                                      uncertainty="motion blur"),)
    _result, _gemini, specialist = _run(
        gemini=_scripted_gemini(_GeminiScript(
            reduce_result=_reduce_result(requests=requests)))
    )
    documents = {bounds: json.loads(doc) for bounds, doc in specialist.calls}
    assert documents[(80, 90)]["reduce_rationale"] == "dense on-screen text region"
    assert documents[(80, 90)]["reduce_uncertainty"] == "motion blur"
    assert documents[(10, 20)]["reduce_rationale"] is None
    assert documents[(10, 20)]["reduce_uncertainty"] is None


def test_caller_windows_cannot_masquerade_as_provider_targets() -> None:
    with pytest.raises(ValidationError):
        _request(specialist_requests=(ReviewWindow(start_frame=80, end_frame=90),))


# ------------------------------------------------------------ selection contracts


def test_advisory_before_hard_reserves_the_hard_target_first() -> None:
    """An early advisory window must never starve a later hard window: hard
    targets are globally reserved before any advisory selection, so the run
    succeeds with the hard target selected and the advisory deferred."""

    advisory = DeepReviewWindow(start_frame=0, end_frame=20,
                                trigger_reason="uncertain", trigger_source="shot-a")
    hard = DeepReviewWindow(start_frame=20, end_frame=30,
                            trigger_reason="human_request", trigger_source="shot-hard")
    selection = select_specialist_targets(TOTAL_FRAMES, (advisory, hard), (),
                                          frame_rate=RATE)
    assert [(w.start_frame, w.end_frame) for w in selection.selected] == [(20, 30)]
    assert [(w.start_frame, w.end_frame) for w in selection.deferred] == [(0, 20)]

    result, _gemini, specialist = _run(progressive_windows=(advisory, hard))
    assert [(w.start_frame, w.end_frame)
            for w in result.targeted_budget.deep_review_windows] == [(20, 30)]
    assert [bounds for bounds, _doc in specialist.calls] == [(20, 30)]


def test_editorial_qc_flag_and_recall_audit_are_hard_obligations() -> None:
    qc = DeepReviewWindow(start_frame=0, end_frame=30, trigger_reason="editorial_qc_flag",
                          trigger_source="shot-qc")
    with pytest.raises((SpecialistSelectionError, BudgetExpansionUnjustifiedError)):
        select_specialist_targets(TOTAL_FRAMES, (qc,), (), frame_rate=RATE)

    audit = DeepReviewWindow(start_frame=0, end_frame=30, trigger_reason="recall_audit_sample",
                             trigger_source="shot-audit")
    with pytest.raises((SpecialistSelectionError, BudgetExpansionUnjustifiedError)):
        select_specialist_targets(TOTAL_FRAMES, (audit,), (), frame_rate=RATE)

    uncertain = DeepReviewWindow(start_frame=0, end_frame=30, trigger_reason="uncertain",
                                 trigger_source="shot-b")
    selection = select_specialist_targets(TOTAL_FRAMES, (uncertain,), (), frame_rate=RATE)
    assert selection.selected == ()
    assert [(w.start_frame, w.end_frame) for w in selection.deferred] == [(0, 30)]


def test_no_specialist_targets_yields_zero_specialist_stages() -> None:
    result, _gemini, specialist = _run(progressive_windows=())
    assert specialist.calls == []
    assert result.specialist_stages == ()
    for review in result.reviews:
        assert _purposes(review) == ("local_map", "global_reduce", "fusion")


# ------------------------------------------------------------ specialist failures


def test_hard_specialist_failure_blocks_with_typed_error() -> None:
    specialist = _FakeSpecialist(
        load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH), fail=frozenset({(10, 20)})
    )
    deps, _gemini, fake_specialist = _deps(specialist=specialist)

    with pytest.raises(HardSpecialistTargetError) as error:
        run_video_understanding(_request(), deps)
    assert "[10, 20)" in error.value.detail
    assert "provider-transport" in error.value.detail
    assert "stable fake transport failure" not in error.value.detail
    assert len(fake_specialist.calls) == 1


def test_advisory_failure_requires_fusion_acknowledgement() -> None:
    """The fusion typed output must explicitly acknowledge every unresolved
    advisory range: with the acknowledgement present the run continues with
    the fusion's own confidence plus the deterministic gap rationale; with
    it omitted the run blocks typed."""

    specialist = _FakeSpecialist(
        load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH), fail=frozenset({(50, 55)})
    )
    result, fake_gemini, _specialist = _run(
        gemini=_scripted_gemini(_GeminiScript(fusion_acks=GAP_ACK)), specialist=specialist
    )

    failed = [s for s in result.specialist_stages if s.outcome == "failed"]
    assert len(failed) == 1
    assert failed[0].analyzed_start_frame is None
    assert failed[0].output_sha256 is None
    overlapped, clean = result.reviews[1], result.reviews[0]
    assert any("[50, 55)" in candidate
               for candidate in overlapped.assessment.remove_rationale_candidates)
    assert clean.assessment.remove_rationale_candidates == ()
    assert _stages(overlapped, "specialist")[0].outcome == "failed"
    fusion_doc = next(doc for purpose, bounds, doc in fake_gemini.calls
                      if purpose == "fusion" and bounds == (30, 70))
    unresolved = json.loads(fusion_doc)["unresolved"]
    assert any(entry["start_frame"] == 50 for entry in unresolved)

    def unacknowledging(fusion: GeminiFusionReview) -> GeminiFusionReview:
        return fusion.model_copy(update={"unresolved_acknowledgements": ()})

    deps, _g, _s = _deps(
        gemini=_scripted_gemini(_GeminiScript(fusion_transform=unacknowledging)),
        specialist=_FakeSpecialist(
            load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH),
            fail=frozenset({(50, 55)}),
        ),
    )
    with pytest.raises(VideoUnderstandingError, match="unacknowledged"):
        run_video_understanding(_request(), deps)


def test_specialist_range_mismatch_blocks_regardless_of_priority() -> None:
    specialist = _FakeSpecialist(
        load_editorial_pin(MOMENT_REVIEW_SPECIALIST_PIN_PATH), mismatch=(50, 55)
    )
    deps, _gemini, _fake_specialist = _deps(specialist=specialist)
    with pytest.raises(StageRangeMismatchError):
        run_video_understanding(_request(), deps)


def test_advisory_targets_beyond_budget_are_deferred_not_gaps() -> None:
    windows = tuple(
        DeepReviewWindow(start_frame=start, end_frame=start + 20,
                         trigger_reason="uncertain", trigger_source=f"shot-{start}")
        for start in (0, 20, 40)
    )
    result, gemini, _specialist = _run(progressive_windows=windows)

    assert tuple((w.start_frame, w.end_frame) for w in result.deferred) == ((20, 40), (40, 60))
    selected = result.targeted_budget.deep_review_windows
    assert tuple((w.start_frame, w.end_frame) for w in selected) == ((0, 20),)
    assert all(r.assessment.remove_rationale_candidates == () for r in result.reviews)
    fusion_doc = json.loads(gemini.calls[-1][2])
    assert any(entry["start_frame"] == 20 for entry in fusion_doc["deferred"])


def test_hard_targets_exceeding_budget_block_the_run() -> None:
    windows = (DeepReviewWindow(start_frame=0, end_frame=30,
                                trigger_reason="human_request",
                                trigger_source="shot-hard"),)
    with pytest.raises((SpecialistSelectionError, BudgetExpansionUnjustifiedError)):
        _run(progressive_windows=windows)


# ------------------------------------------------------------ fusion input


def test_fusion_document_carries_local_reduce_and_observations() -> None:
    _result, gemini, _specialist = _run()
    purpose, bounds, document = gemini.calls[-1]
    payload = json.loads(document)

    assert purpose == "fusion"
    assert (payload["window"]["start_frame"], payload["window"]["end_frame"]) == bounds
    assert payload["local"]["summary"].startswith("local summary")
    assert payload["reduce"]["summary"].startswith("reduce summary")
    assert isinstance(payload["specialists"], list)
    assert payload["audio_note"] is not None


def test_transcript_segment_ids_follow_window_overlap() -> None:
    _result, gemini, _specialist = _run()
    docs = {bounds: json.loads(doc) for purpose, bounds, doc in gemini.calls
            if purpose == "fusion"}
    assert docs[(0, 40)]["transcript_segment_ids"] == ["tr-1"]
    assert docs[(30, 70)]["transcript_segment_ids"] == ["tr-1"]
    assert docs[(60, 100)]["transcript_segment_ids"] == []


def test_fusion_sub_span_outside_window_blocks_typed() -> None:
    def drifting(fusion: GeminiFusionReview) -> GeminiFusionReview:
        return fusion.model_copy(
            update={"best_sub_span": FusionSubSpan(start_frame=90, end_frame=99)})

    gemini = _scripted_gemini(_GeminiScript(fusion_transform=drifting))
    deps, _g, _s = _deps(gemini=gemini)
    with pytest.raises(VideoUnderstandingError, match="sub-span"):
        run_video_understanding(_request(), deps)


# ------------------------------------------------------------ seam discipline


def test_module_exposes_no_editorial_intent_or_commit_surface() -> None:
    source = inspect.getsource(vu_module)
    forbidden_defs = re.compile(
        r"^(\s*)?(async )?def (mutate|apply|commit|write|insert|update|delete|patch)\w*",
        re.MULTILINE,
    )
    assert forbidden_defs.search(source) is None
    assert "services.resolve" not in source
    assert "plan_progressive_analysis" not in source  # consumes triggers; never plans
    for name in dir(vu_module):
        if name.startswith("_"):
            continue
        assert not re.search(r"commit|director|resolve|edit_plan|selection_plan", name), name


def test_overlapping_windows_stay_distinct_and_deterministically_ordered() -> None:
    result, _gemini, _specialist = _run()
    bounds = [(r.source_window.start_frame, r.source_window.end_frame) for r in result.reviews]
    assert len({r.review_id for r in result.reviews}) == 3
    assert bounds == sorted(set(bounds))
    rows = review_rows(result.reviews)
    assert len(rows) == 3
    assert all(len(row) == 11 for row in rows)  # primary columns unchanged
    assert [row[3] for row in rows] == [0, 30, 60]  # start_frame ordered


# ------------------------------------------------------------ downstream acceptance


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
        sources=(MediaSource(source_id="src-cam-a", duration_frames=TOTAL_FRAMES),),
        shots=(shot("shot-a", 0, 40), shot("shot-b", 40, 70), shot("shot-c", 70, 100)),
    )


def test_fused_reviews_are_indexable_and_carry_real_lineage(tmp_path: Path) -> None:
    result, _gemini, _specialist = _run()
    reviews = result.reviews
    require_real_lineage(reviews)

    index_path = build_index(_mini_artifact(), tmp_path / "t6-v2.duckdb", reviews=reviews)
    with MediaQueryApiV2.open(index_path) as api:
        response = api.moment_reviews(
            vm.MomentReviewsRequest(
                span=vm.FrameSpan(start_frame=45, end_frame=46),
                pagination=vm.V2Pagination(limit=50, offset=0),
            )
        )
        assert response.total == 1  # only [30, 70) overlaps frame 45
        assert response.rows[0].review_id == reviews[1].review_id
        assert response.rows[0].provider == load_editorial_pin(
            MOMENT_REVIEW_PIN_PATH
        ).api_surface
