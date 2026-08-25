"""Unit tests for the real V44-0 arm pipeline (no codex, no media toolchain).

The DirectorV2 llm seam is driven by the deterministic planner applied to
each pass REQUEST (canned payloads that exercise the real model-validation
path in ``director_v2`` — unknown-candidate / uncorroborated-keep guards),
and the arm-B assessment provider is a fake whose confidence drives the
escalation policy.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.cli.real_pool import SpeechSegment
from services.cli.v44_arm_evidence import (
    ArmEvidenceError,
    compose_arm_brief,
    compute_arm_evidence_quality,
    escalated_anchor_ids,
    mezz_span_to_anchor_space,
)
from services.cli.v44_arm_pipeline import (
    ESCALATE_BELOW,
    ArmPipelineInputs,
    run_arm_pipeline,
)
from services.cli.v44_arm_stages import (
    ArmPipelineData,
    ArmPipelineError,
    whisper_provider_pin,
)
from services.cli.v44_product_proof import main as cli_main
from services.editorial_v2.heuristic_planner import (
    plan_creative,
    plan_selection,
    plan_story,
)
from services.editorial_v2.prompt_v2 import PassARequest, PassBRequest
from services.media_intelligence.moment_review import (
    AudioContext,
    BestSubSpan,
    FrameBundleEntry,
    MomentAssessment,
    ReviewConfidence,
    ReviewWindow,
)
from services.media_intelligence.moment_review_real import (
    AssessmentOutcome,
    RealReviewProviders,
    TranscriptSegmentText,
)
from services.metrics.v44_product_proof import (
    EditorialGroundTruthV1,
    GroundTruthAnchor,
    TranscriptSampleV1,
)
from services.metrics.v44_product_proof import (
    TranscriptSegment as SampleSegment,
)

if TYPE_CHECKING:
    from services.editorial_v2.prompt_v2 import PassName
    from services.media_intelligence.moment_review_real import (
        AssessmentEvidenceBundle,
        AssessmentProvider,
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


def _inputs(tmp_path: Path, assessment: AssessmentProvider | None = None) -> ArmPipelineInputs:
    analysis = _analysis()
    return ArmPipelineInputs(
        episode_root=tmp_path / "episode",
        workspace=tmp_path / "workspace",
        episode_id=analysis.episode_id,
        brief=compose_arm_brief(tmp_path / "episode", analysis.episode_id, "suda"),
        llm_call=_planner_fake,
        assessment=assessment,
        analysis=analysis,
    )


class _FakeFrames:
    def extract(self, window: ReviewWindow) -> tuple[FrameBundleEntry, ...]:
        return (
            FrameBundleEntry(
                frame=window.start_frame, ref="file:///evidence/frame-stub.png"
            ),
        )


class _FakeTranscripts:
    def segments(self, window: ReviewWindow) -> tuple[TranscriptSegmentText, ...]:
        return ()

    def overlapping(self, window: ReviewWindow) -> tuple[str, ...]:
        return ()


class _FakeAudio:
    def context(self, window: ReviewWindow) -> AudioContext:
        return AudioContext(note="fake quiet window")


class _FakeAssessment:
    """Real-protocol assessment provider; low confidence to force escalation."""

    def __init__(self, overall: float) -> None:
        self._overall = overall
        self.seen_windows: list[ReviewWindow] = []

    def assess(self, bundle: AssessmentEvidenceBundle) -> AssessmentOutcome:
        window = bundle.window
        self.seen_windows.append(window)
        return AssessmentOutcome(
            assessment=MomentAssessment(
                subject_action_evolution="fake: action",
                reaction_notes="fake: reaction",
                timing_notes="fake: timing",
                best_sub_span=BestSubSpan(
                    start_frame=window.start_frame, end_frame=window.end_frame
                ),
                keep_rationale_candidates=("fake: keep",),
                remove_rationale_candidates=(),
                cut_in_handle="in",
                cut_out_handle="out",
            ),
            confidence=ReviewConfidence(
                overall=self._overall,
                subject_action_evolution=self._overall,
                reaction_notes=self._overall,
                timing_notes=self._overall,
                best_sub_span=self._overall,
            ),
            cost=None,
        )


def _fake_providers() -> RealReviewProviders:
    return RealReviewProviders(
        frames=_FakeFrames(), transcripts=_FakeTranscripts(), audio=_FakeAudio()
    )


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


def test_arm_b_low_confidence_reviews_demote_and_escalate(tmp_path: Path) -> None:
    assessment = _FakeAssessment(ESCALATE_BELOW - 0.1)
    inputs = replace(
        _inputs(tmp_path, assessment=assessment), review_providers=_fake_providers()
    )
    result = run_arm_pipeline(inputs)

    assert 1 <= len(result.reviews) <= 3
    assert assessment.seen_windows, "reviews must actually call the provider"
    review_files = list((inputs.workspace / "moment-review").glob("*.json"))
    assert len(review_files) == len(result.reviews)
    for review in result.reviews:
        assert review.lineage.provider == "codex-exec"
    escalated = result.escalated_candidate_ids
    assert escalated, "low-confidence keeps must escalate"
    assert result.kept_spans_mezz == ()
    assert len(escalated) == len(result.reviews)
    assert any(
        "arm_b_escalation" in note for note in result.notes
    ), "the escalation policy must be recorded in the notes"

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


def test_arm_b_high_confidence_reviews_reconfirm_keeps(tmp_path: Path) -> None:
    assessment = _FakeAssessment(0.9)
    inputs = replace(_inputs(tmp_path, assessment=assessment), review_providers=_fake_providers())
    result = run_arm_pipeline(inputs)

    assert result.reviews
    assert result.escalated_candidate_ids == ()
    assert result.kept_spans_mezz, "re-confirmed keeps stay kept"


def test_mezz_to_anchor_space_conversion() -> None:
    assert mezz_span_to_anchor_space(0, 90) == (0, 90)
    assert mezz_span_to_anchor_space(3000, 3009) == (2997, 3006)
    assert mezz_span_to_anchor_space(8469, 8469) == (8461, 8461)


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
