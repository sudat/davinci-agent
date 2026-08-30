from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.cli._v44_finishing_report import (
    QUALITY_DOMAIN_NAMES,
    FinishingEditorialQcBlockV1,
    FinishingQcBlockV1,
    FinishingRunReportV1,
    TimeLogLineV1,
    TimeLogPhase,
    summarize_time_log,
)
from services.cli.v44_product_proof import main as cli_main
from services.foundation_io import canonical_model_bytes
from services.media_intelligence.moment_review import (
    AudioContext,
    BestSubSpan,
    FrameBundleEntry,
    MomentAssessment,
    MomentDeepReviewV1,
    ReviewConfidence,
    ReviewEvidence,
    ReviewLineage,
    ReviewRecordRequest,
    ReviewStageLineage,
    ReviewWindow,
    record_review,
)
from services.media_intelligence.moment_review_real import (
    SyntheticLineageError,
    require_real_lineage,
)
from services.metrics.v44_gate_state import (
    ObservationStageTimelineV1,
    V1ObservationRecord,
)
from services.metrics.v44_product_proof import (
    AnchorLabel,
    EditorialGroundTruthV1,
    EfficiencyMetrics,
    EpisodeContext,
    EvaluationBindingError,
    EvaluationBindingV1,
    GroundTruthAnchor,
    OperatorVerdict,
    PassPolicyBlock,
    ProductProofReportV1,
    RunIdentity,
    TranscriptSampleV1,
    TranscriptSegment,
    build_progressive_report,
    catastrophic_removal_count,
    cer,
    compute_editorial_metrics,
    compute_evidence_quality,
    duplicated_utterance_count,
    evaluate_pass_policy,
    must_keep_recall,
    must_remove_retention,
    omitted_utterance_count,
    proper_noun_recall,
    redundancy_errors,
    run_arm_a,
    run_arm_b,
    run_arm_c,
    timestamp_error_p95_ms,
)
from services.metrics.v44_video_understanding_metrics import (
    VideoUnderstandingMetrics,
    compute_video_understanding_metrics,
)


def _anchor(
    anchor_id: str, s: int, e: int, label: AnchorLabel, note: str | None = None
) -> GroundTruthAnchor:
    return GroundTruthAnchor(
        anchor_id=anchor_id, start_frame=s, end_frame=e, label=label, note=note
    )


def _eval_binding(
    gt_sha: str = "c" * 64,
    label: str = "v2",
    lane: str = "operator_corrected_diagnostic",
    transcript_sha: str = "d" * 64,
) -> EvaluationBindingV1:
    return EvaluationBindingV1(
        ground_truth_sha256=gt_sha,  # type: ignore[arg-type]
        ground_truth_label=label,
        evidence_lane=lane,  # type: ignore[arg-type]
        transcript_sha256=transcript_sha,  # type: ignore[arg-type]
    )


def test_must_keep_recall_with_escalation() -> None:
    anchors = (
        _anchor("a-001", 0, 100, "must_keep"),
        _anchor("a-002", 200, 300, "must_keep"),
        _anchor("a-003", 400, 500, "must_keep"),
    )
    kept = [(0, 100), (200, 300)]  # 2 recalled via span
    escalated = ["a-003"]  # 1 escalated
    assert must_keep_recall(anchors, kept, escalated) == 1.0
    assert catastrophic_removal_count(anchors, kept, escalated) == 0
    metrics = compute_editorial_metrics(anchors, kept, escalated)
    assert metrics.must_keep_recall == 1.0
    assert metrics.must_keep_recalled == 3
    assert metrics.catastrophic_removal_count == 0


def test_catastrophic_removal_policy_fails() -> None:
    anchors = (_anchor("a-001", 0, 100, "must_keep"),)
    kept: list[tuple[int, int]] = []  # confidently removed
    assert catastrophic_removal_count(anchors, kept, []) == 1
    metrics = compute_editorial_metrics(anchors, kept, [])
    assert metrics.must_keep_recall == 0.0
    # Build a report to test policy
    ctx = EpisodeContext(
        episode_id="test-ep-001",
        ground_truth=EditorialGroundTruthV1(
            episode_id="test-ep-001",
            anchors=anchors,
            created_at="2026-08-23T00:00:00+00:00",
            operator="op",
        ),
        kept_spans=tuple(kept),
    )
    result = run_arm_a(ctx)
    policy = evaluate_pass_policy(result.report)
    assert policy.passed is False
    assert (
        "catastrophic_removal" in policy.failed_criteria
        or "must_keep_recall" in policy.failed_criteria
    )


def test_must_remove_retention_boundary() -> None:
    anchors = (
        _anchor("r-001", 0, 100, "must_remove"),
        _anchor("r-002", 200, 300, "must_remove"),
        _anchor("r-003", 400, 500, "must_remove"),
        _anchor("r-004", 600, 700, "must_remove"),
    )
    kept = [(0, 100)]  # 1 of 4 retained = 0.25 exactly
    assert must_remove_retention(anchors, kept) == 0.25
    metrics = compute_editorial_metrics(anchors, kept)
    assert metrics.must_remove_retention == 0.25
    # Policy should pass retention at boundary (<=0.25)
    # Build report with perfect must_keep recall and operator YES to isolate retention
    mk_anchors = (_anchor("k-001", 1000, 1100, "must_keep"),)
    all_anchors = mk_anchors + anchors
    mk_kept = [(1000, 1100), (0, 100)]
    m = compute_editorial_metrics(all_anchors, mk_kept)
    assert m.must_remove_retention == 0.25
    # With operator YES, retention 0.25 should not be in failed_criteria
    report = ProductProofReportV1(
        run=RunIdentity(
            episode_id="test-ep-002",
            commit_sha="a" * 40,
            run_kind="v44-0",
            model_pin="m",
            analysis_provider_pin="a",
        ),  # type: ignore[arg-type]
        editorial=m,
        operator=OperatorVerdict(continuation_yes_no=True, publishability="as_is"),
    )
    policy = evaluate_pass_policy(report)
    assert "must_remove_retention" not in policy.failed_criteria


def test_must_remove_retention_exceeds_boundary() -> None:
    anchors = (
        _anchor("r-001", 0, 100, "must_remove"),
        _anchor("r-002", 200, 300, "must_remove"),
        _anchor("r-003", 400, 500, "must_remove"),
        _anchor("r-004", 600, 700, "must_remove"),
    )
    kept = [(0, 100), (200, 300)]  # 2/4 = 0.5 > 0.25
    assert must_remove_retention(anchors, kept) == 0.5

    m = compute_editorial_metrics(anchors, kept)
    report = ProductProofReportV1(
        run=RunIdentity(
            episode_id="test-ep-003",
            commit_sha="a" * 40,
            run_kind="v44-0",
            model_pin="m",
            analysis_provider_pin="a",
        ),  # type: ignore[arg-type]
        editorial=m,
        operator=OperatorVerdict(continuation_yes_no=True, publishability="as_is"),
    )
    policy = evaluate_pass_policy(report)
    assert "must_remove_retention" in policy.failed_criteria


def test_redundancy_errors() -> None:
    anchors = (
        _anchor("r-001", 0, 100, "must_remove", note="冗長な言い直し"),
        _anchor("r-002", 200, 300, "must_remove", note="redundant statement"),
        _anchor("r-003", 400, 500, "must_remove", note="boring but okay"),
    )
    kept = [(0, 100), (200, 300)]  # both redundancy anchors kept
    assert redundancy_errors(anchors, kept) == 2
    kept2 = [(400, 500)]  # only non-redundancy kept
    assert redundancy_errors(anchors, kept2) == 0


def test_cer_identical_and_substitution() -> None:
    assert cer("hello", "hello") == 0.0
    # one substitution in 10 chars -> 0.1
    ref = "abcdefghij"  # 10 chars
    hyp = "abcdeXghij"  # 1 substitution at position 5
    assert cer(ref, hyp) == pytest.approx(0.1)
    # empty ref
    assert cer("", "") == 0.0
    assert cer("", "x") == 1.0


def test_proper_noun_recall() -> None:
    expected = {"p1": "田中太郎", "p2": "東京タワー", "p3": "DaVinci"}
    actual = {"p1": "田中太郎", "p2": "東京タワー", "p3": "davinci"}  # last mismatch case sensitive
    assert proper_noun_recall(expected, actual) == pytest.approx(2 / 3)
    assert proper_noun_recall({}, {}) == 1.0
    assert proper_noun_recall({"a": "x"}, {}) == 0.0


def test_timestamp_error_p95() -> None:
    assert timestamp_error_p95_ms([]) is None
    # n=20, p95 rank = ceil(19) =19 -> 19th smallest (1-indexed)
    diffs = [float(i) for i in range(20)]
    assert timestamp_error_p95_ms(diffs) == 18.0
    diffs2 = [10.0, 20.0, 30.0, 40.0]
    # n=4, rank=ceil(3.8)=4 -> max
    assert timestamp_error_p95_ms(diffs2) == 40.0


def test_omitted_and_duplicated_counts() -> None:
    corrected = TranscriptSampleV1(
        segments=(
            TranscriptSegment(start_ms=0, end_ms=1000, text="こんにちは"),
            TranscriptSegment(start_ms=1000, end_ms=2000, text="冗長な文"),
            TranscriptSegment(start_ms=2000, end_ms=3000, text="さようなら"),
        ),
        proper_nouns={},
    )
    hypothesis = TranscriptSampleV1(
        segments=(
            TranscriptSegment(start_ms=0, end_ms=1000, text="こんにちは"),
            TranscriptSegment(start_ms=1000, end_ms=2000, text="こんにちは"),  # duplicate
        ),
        proper_nouns={},
    )
    assert omitted_utterance_count(corrected, hypothesis) == 2  # 冗長な文, さようなら missing
    assert duplicated_utterance_count(corrected, hypothesis) == 1  # extra こんにちは


def test_compute_evidence_quality_no_speech() -> None:
    corrected = TranscriptSampleV1(segments=(), proper_nouns={})
    eq = compute_evidence_quality(corrected, ())
    assert eq.transcript_cer is None
    assert eq.omitted_utterances is None


def test_policy_with_operator_fields_null_pending() -> None:
    anchors = (_anchor("k-001", 0, 100, "must_keep"),)
    kept = [(0, 100)]
    m = compute_editorial_metrics(anchors, kept)
    report = ProductProofReportV1(
        run=RunIdentity(
            episode_id="test-ep-004",
            commit_sha="a" * 40,
            run_kind="v44-0-arm",
            model_pin="m",
            analysis_provider_pin="a",
        ),  # type: ignore[arg-type]
        editorial=m,
        operator=OperatorVerdict(continuation_yes_no=None, publishability=None),
    )
    result = evaluate_pass_policy(report)
    assert result.passed is False
    assert "operator_continuation_yes_no" in result.pending_criteria
    assert "operator_publishability" in result.pending_criteria


def test_policy_passes_when_all_good() -> None:
    anchors = (_anchor("k-001", 0, 100, "must_keep"),)
    kept = [(0, 100)]
    m = compute_editorial_metrics(anchors, kept)

    report = ProductProofReportV1(
        run=RunIdentity(
            episode_id="test-ep-005",
            commit_sha="a" * 40,
            run_kind="v44-0",
            model_pin="m",
            analysis_provider_pin="a",
        ),  # type: ignore[arg-type]
        editorial=m,
        operator=OperatorVerdict(continuation_yes_no=True, publishability="as_is", comments="ok"),
    )
    result = evaluate_pass_policy(report)
    assert result.passed is True
    assert result.failed_criteria == ()
    assert result.pending_criteria == ()


def test_ground_truth_init_validate_round_trip(tmp_path: Path) -> None:
    gt = EditorialGroundTruthV1(
        episode_id="test-ep-006",
        anchors=(_anchor("a-001", 0, 100, "must_keep", note="ok"),),
        created_at="2026-08-23T00:00:00+00:00",
        operator="tester",
    )
    out = tmp_path / "gt.json"
    out.write_bytes(canonical_model_bytes(gt))
    loaded = EditorialGroundTruthV1.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert loaded == gt
    assert loaded.continuation_question == "この選択なら続きを作る価値があるか"


def test_ground_truth_overlap_rejected() -> None:
    with pytest.raises(ValidationError, match="must_keep_overlap"):
        EditorialGroundTruthV1(
            episode_id="test-ep-007",
            anchors=(
                _anchor("a-001", 0, 200, "must_keep"),
                _anchor("a-002", 100, 300, "must_keep"),
            ),
            created_at="2026-08-23T00:00:00+00:00",
            operator="op",
        )


def test_ground_truth_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        EditorialGroundTruthV1(
            episode_id="test-ep-008",
            anchors=(_anchor("a-001", 200, 100, "must_keep"),),  # end <= start
            created_at="2026-08-23T00:00:00+00:00",
            operator="op",
        )


def test_ground_truth_duplicate_anchor_id_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate_anchor_id"):
        EditorialGroundTruthV1(
            episode_id="test-ep-009",
            anchors=(
                _anchor("dup", 0, 100, "must_keep"),
                _anchor("dup", 200, 300, "must_keep"),
            ),
            created_at="2026-08-23T00:00:00+00:00",
            operator="op",
        )


def test_transcript_sample_validation() -> None:
    with pytest.raises(ValidationError):
        TranscriptSegment(start_ms=200, end_ms=100, text="bad")
    s = TranscriptSampleV1(
        segments=(TranscriptSegment(start_ms=0, end_ms=1000, text="hello"),),
        proper_nouns={"p1": "田中"},
    )
    assert s.segments[0].text == "hello"


def test_arm_b_with_fake_real_lineage_reviews() -> None:
    gt = EditorialGroundTruthV1(
        episode_id="test-ep-010",
        anchors=(_anchor("a-001", 0, 100, "must_keep"), _anchor("a-002", 200, 300, "must_keep")),
        created_at="2026-08-23T00:00:00+00:00",
        operator="op",
    )
    ctx = EpisodeContext(
        episode_id="test-ep-010", ground_truth=gt, kept_spans=((0, 100), (200, 300))
    )

    def fake_llm(pass_name: str, request: object) -> object:
        return {"story_plan": {"title": "t"}}

    # Construct real-lineage reviews directly
    reviews = []
    for anchor in gt.anchors:
        window = ReviewWindow(start_frame=int(anchor.start_frame), end_frame=int(anchor.end_frame))
        evidence = ReviewEvidence(
            frame_bundle=(
                FrameBundleEntry(frame=int(anchor.start_frame), ref="file:///tmp/fake.png"),
            ),
            transcript_refs=(),
            audio_context=AudioContext(note="real"),
        )
        assessment = MomentAssessment(
            subject_action_evolution="real",
            reaction_notes="real",
            timing_notes="real",
            best_sub_span=BestSubSpan(
                start_frame=int(anchor.start_frame), end_frame=int(anchor.end_frame)
            ),
            keep_rationale_candidates=("keep",),
            remove_rationale_candidates=(),
            cut_in_handle="in",
            cut_out_handle="out",
        )
        rec = record_review(
            ReviewRecordRequest(
                episode_id="test-ep-010",  # type: ignore[arg-type]
                source_duration_frames=1000,
                window=window,
                evidence=evidence,
                assessment=assessment,
                confidence=ReviewConfidence(
                    overall=0.9,
                    subject_action_evolution=0.9,
                    reaction_notes=0.9,
                    timing_notes=0.9,
                    best_sub_span=0.9,
                ),
                lineage=ReviewLineage(
                    provider="openai",
                    provider_version="gpt-5.6-sol",
                    tool="multimodal-v1",
                    cost=0.01,
                ),
            )
        )
        reviews.append(rec)

    result = run_arm_b(ctx, reviews, llm_call=fake_llm)
    assert result.report.editorial is not None
    assert result.report.editorial.must_keep_recall == 1.0
    assert len(result.deep_reviews) == 2
    # Real lineage should pass gate
    require_real_lineage(reviews)


def test_require_real_lineage_raises_on_synthetic() -> None:
    window = ReviewWindow(start_frame=0, end_frame=100)
    evidence = ReviewEvidence(
        frame_bundle=(FrameBundleEntry(frame=0, ref="synthetic://frame/0"),),
        transcript_refs=(),
        audio_context=AudioContext(note="synthetic"),
    )
    assessment = MomentAssessment(
        subject_action_evolution="synthetic",
        reaction_notes="synthetic",
        timing_notes="synthetic",
        best_sub_span=BestSubSpan(start_frame=0, end_frame=100),
        keep_rationale_candidates=("keep",),
        remove_rationale_candidates=(),
        cut_in_handle="in",
        cut_out_handle="out",
    )
    rec = record_review(
        ReviewRecordRequest(
            episode_id="test-ep-011",  # type: ignore[arg-type]
            source_duration_frames=1000,
            window=window,
            evidence=evidence,
            assessment=assessment,
            confidence=ReviewConfidence(
                overall=0.5,
                subject_action_evolution=0.5,
                reaction_notes=0.5,
                timing_notes=0.5,
                best_sub_span=0.5,
            ),
            lineage=ReviewLineage(
                provider="synthetic", provider_version="v0", tool="synthetic", cost=None
            ),
        )
    )
    with pytest.raises(SyntheticLineageError):
        require_real_lineage([rec])
    # Also with synthetic-local
    assessment2 = MomentAssessment(
        subject_action_evolution="synthetic",
        reaction_notes="synthetic",
        timing_notes="synthetic",
        best_sub_span=BestSubSpan(start_frame=100, end_frame=200),
        keep_rationale_candidates=("keep",),
        remove_rationale_candidates=(),
        cut_in_handle="in",
        cut_out_handle="out",
    )
    rec2 = record_review(
        ReviewRecordRequest(
            episode_id="test-ep-011",  # type: ignore[arg-type]
            source_duration_frames=1000,
            window=ReviewWindow(start_frame=100, end_frame=200),
            evidence=ReviewEvidence(
                frame_bundle=(FrameBundleEntry(frame=100, ref="synthetic://frame/100"),),
                transcript_refs=(),
                audio_context=AudioContext(note="synthetic"),
            ),
            assessment=assessment2,
            confidence=ReviewConfidence(
                overall=0.5,
                subject_action_evolution=0.5,
                reaction_notes=0.5,
                timing_notes=0.5,
                best_sub_span=0.5,
            ),
            lineage=ReviewLineage(
                provider="synthetic-local", provider_version="v0", tool="synthetic", cost=None
            ),
        )
    )
    with pytest.raises(SyntheticLineageError):
        require_real_lineage([rec2])


def test_arm_b_rejects_synthetic_reviews() -> None:
    gt = EditorialGroundTruthV1(
        episode_id="test-ep-012",
        anchors=(_anchor("a-001", 0, 100, "must_keep"),),
        created_at="2026-08-23T00:00:00+00:00",
        operator="op",
    )
    ctx = EpisodeContext(episode_id="test-ep-012", ground_truth=gt, kept_spans=((0, 100),))
    window = ReviewWindow(start_frame=0, end_frame=100)
    evidence = ReviewEvidence(
        frame_bundle=(FrameBundleEntry(frame=0, ref="synthetic://frame/0"),),
        transcript_refs=(),
        audio_context=AudioContext(note="synthetic"),
    )
    assessment = MomentAssessment(
        subject_action_evolution="synthetic",
        reaction_notes="synthetic",
        timing_notes="synthetic",
        best_sub_span=BestSubSpan(start_frame=0, end_frame=100),
        keep_rationale_candidates=("keep",),
        remove_rationale_candidates=(),
        cut_in_handle="in",
        cut_out_handle="out",
    )
    rec = record_review(
        ReviewRecordRequest(
            episode_id="test-ep-012",  # type: ignore[arg-type]
            source_duration_frames=1000,
            window=window,
            evidence=evidence,
            assessment=assessment,
            confidence=ReviewConfidence(
                overall=0.5,
                subject_action_evolution=0.5,
                reaction_notes=0.5,
                timing_notes=0.5,
                best_sub_span=0.5,
            ),
            lineage=ReviewLineage(
                provider="synthetic", provider_version="v0", tool="synthetic", cost=None
            ),
        )
    )
    with pytest.raises(SyntheticLineageError):
        run_arm_b(ctx, [rec])


def test_arm_c_classification() -> None:
    gt = EditorialGroundTruthV1(
        episode_id="test-ep-013",
        anchors=(_anchor("a-001", 0, 100, "must_keep"),),
        created_at="2026-08-23T00:00:00+00:00",
        operator="op",
    )
    ctx = EpisodeContext(episode_id="test-ep-013", ground_truth=gt, kept_spans=())
    failed = [gt.anchors[0]]
    result = run_arm_c(ctx, {"corrected_text": "fix"}, failed, corrected_kept_spans=[(0, 100)])
    assert result.report.notes is not None
    assert "evidence_failure" in result.report.notes
    # Reasoning failure when corrected still not kept
    result2 = run_arm_c(ctx, {"corrected_text": "fix"}, failed, corrected_kept_spans=[])
    assert "reasoning_failure" in result2.report.notes  # type: ignore[union-attr]


def test_record_operator_verdict_round_trip(tmp_path: Path) -> None:
    gt = EditorialGroundTruthV1(
        episode_id="test-ep-014",
        anchors=(_anchor("a-001", 0, 100, "must_keep"),),
        created_at="2026-08-23T00:00:00+00:00",
        operator="op",
    )
    ctx = EpisodeContext(episode_id="test-ep-014", ground_truth=gt, kept_spans=((0, 100),))
    result = run_arm_a(ctx)
    report_path = tmp_path / "report.json"
    report_path.write_bytes(canonical_model_bytes(result.report))

    # Record verdict via CLI main
    rc = cli_main(
        [
            "record-operator-verdict",
            "--report",
            str(report_path),
            "--continuation",
            "yes",
            "--publishability",
            "as_is",
            "--comments",
            "good",
        ]
    )
    assert rc == 0
    loaded = ProductProofReportV1.model_validate(
        json.loads(report_path.read_text(encoding="utf-8"))
    )
    assert loaded.operator.continuation_yes_no is True
    assert loaded.operator.publishability == "as_is"
    assert loaded.operator.comments == "good"
    # Re-validate on second record (stale_state guard)
    rc2 = cli_main(
        [
            "record-operator-verdict",
            "--report",
            str(report_path),
            "--continuation",
            "no",
            "--publishability",
            "not_yet",
        ]
    )
    assert rc2 == 0
    loaded2 = ProductProofReportV1.model_validate(
        json.loads(report_path.read_text(encoding="utf-8"))
    )
    assert loaded2.operator.continuation_yes_no is False


def test_cli_init_validate_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "gt.json"
    rc = cli_main(["init-ground-truth", "--episode-id", "test-ep-015", "--out", str(out)])
    assert rc == 0
    assert out.is_file()
    rc2 = cli_main(["validate-ground-truth", "--path", str(out)])
    assert rc2 == 0
    # Invalid file should fail
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "schema_version": "editorial-ground-truth-v1",
                "episode_id": "x",
                "anchors": [
                    {"anchor_id": "a", "start_frame": 100, "end_frame": 50, "label": "must_keep"}
                ],
                "created_at": "now",
                "operator": "op",
            }
        ),
        encoding="utf-8",
    )
    rc3 = cli_main(["validate-ground-truth", "--path", str(bad)])
    assert rc3 == 2


def test_cli_run_arm_dry_run(tmp_path: Path) -> None:
    # Create ground truth
    gt = EditorialGroundTruthV1(
        episode_id="test-ep-016",
        anchors=(_anchor("a-001", 0, 100, "must_keep"),),
        created_at="2026-08-23T00:00:00+00:00",
        operator="op",
    )
    gt_path = tmp_path / "gt.json"
    gt_path.write_bytes(canonical_model_bytes(gt))
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
    report = ProductProofReportV1.model_validate(json.loads(out.read_text(encoding="utf-8")))
    policy = evaluate_pass_policy(report)
    assert policy.passed is False
    assert "operator_continuation_yes_no" in policy.pending_criteria


def test_cli_evaluate(tmp_path: Path) -> None:
    gt = EditorialGroundTruthV1(
        episode_id="test-ep-017",
        anchors=(_anchor("a-001", 0, 100, "must_keep"),),
        created_at="2026-08-23T00:00:00+00:00",
        operator="op",
    )
    ctx = EpisodeContext(episode_id="test-ep-017", ground_truth=gt, kept_spans=((0, 100),))
    report = run_arm_a(ctx).report
    report_path = tmp_path / "report.json"
    report_path.write_bytes(canonical_model_bytes(report))
    rc = cli_main(["evaluate", "--report", str(report_path)])
    assert rc == 0
    sidecar = Path(str(report_path) + ".policy.json")
    assert sidecar.is_file()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert "passed" in data
    assert "pending_criteria" in data


def test_cli_smoke_via_subprocess(tmp_path: Path) -> None:
    out = tmp_path / "gt2.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.cli.v44_product_proof",
            "init-ground-truth",
            "--episode-id",
            "test-ep-018",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0, result.stderr
    result2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.cli.v44_product_proof",
            "validate-ground-truth",
            "--path",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result2.returncode == 0, result2.stderr


# ---------------------------------------------------------------------------
# T8 — additive report fields, timing/proof integration
# ---------------------------------------------------------------------------

_LEAD_PIN = "lead-pin-sha"
_SPEC_PIN = "spec-pin-sha"


def _vu_stage(  # noqa: PLR0913 (lineage fixture mirrors the stage-ledger surface)
    purpose: str,
    start: int,
    end: int,
    *,
    pin: str,
    input_sha: str,
    attempts: int = 1,
    cost: float | None = None,
    ok: bool = True,
) -> ReviewStageLineage:
    return ReviewStageLineage(
        purpose=purpose,  # type: ignore[arg-type]
        provider="provider-test",
        model_id="model-test",
        pin_sha256=pin,
        tool="video-understanding-v1",
        requested_start_frame=start,
        requested_end_frame=end,
        analyzed_start_frame=start if ok else None,
        analyzed_end_frame=end if ok else None,
        input_sha256=input_sha,
        output_sha256="o" * 64 if ok else None,
        attempts=attempts,
        cost=cost,
        outcome="analyzed" if ok else "failed",
    )


def _fused_review(  # noqa: PLR0913 (review fixture mirrors the record surface)
    episode_id: str,
    start: int,
    end: int,
    stages: tuple[ReviewStageLineage, ...],
    *,
    primary_cost: float | None,
    duration: int,
) -> MomentDeepReviewV1:
    window = ReviewWindow(start_frame=start, end_frame=end)
    evidence = ReviewEvidence(
        frame_bundle=(FrameBundleEntry(frame=start, ref=f"file:///tmp/f-{start}.png"),),
        transcript_refs=(),
        audio_context=AudioContext(note="real"),
    )
    assessment = MomentAssessment(
        subject_action_evolution="real",
        reaction_notes="real",
        timing_notes="real",
        best_sub_span=BestSubSpan(start_frame=start, end_frame=end),
        keep_rationale_candidates=("keep",),
        remove_rationale_candidates=(),
        cut_in_handle="in",
        cut_out_handle="out",
    )
    return record_review(
        ReviewRecordRequest(
            episode_id=episode_id,  # type: ignore[arg-type]
            source_duration_frames=duration,
            window=window,
            evidence=evidence,
            assessment=assessment,
            confidence=ReviewConfidence(
                overall=0.9,
                subject_action_evolution=0.9,
                reaction_notes=0.9,
                timing_notes=0.9,
                best_sub_span=0.9,
            ),
            lineage=ReviewLineage(
                provider="google-gemini-test",
                provider_version="gemini-3.7-flash",
                tool="video-understanding-v1",
                cost=primary_cost,
                stage_lineage=stages,
            ),
        )
    )


def test_old_report_payload_parses_with_t8_optional_defaults() -> None:
    anchors = (_anchor("k-001", 0, 100, "must_keep"),)
    m = compute_editorial_metrics(anchors, [(0, 100)])
    report = ProductProofReportV1(
        run=RunIdentity(
            episode_id="test-ep-t8-old",
            commit_sha="a" * 40,
            run_kind="v44-0",
            model_pin="m",
            analysis_provider_pin="a",
        ),  # type: ignore[arg-type]
        editorial=m,
        efficiency=EfficiencyMetrics(wall_clock_seconds=10.0),
    )
    payload = report.model_dump(mode="json")
    # Strip every T8 field: a pre-T8 report payload must still parse.
    del payload["run"]["moment_review_lead_pin"]
    del payload["run"]["moment_review_specialist_pin"]
    del payload["video_understanding"]
    del payload["efficiency"]["direct_resolve_minutes"]
    del payload["pass_policy"]["require_efficiency_timing"]
    # Strip the evaluation binding too: pre-binding (r1-r3) payloads parse
    # with evaluation_binding=None (Task 1 backward compatibility).
    del payload["evaluation_binding"]
    loaded = ProductProofReportV1.model_validate(payload)
    assert loaded.run.moment_review_lead_pin is None
    assert loaded.run.moment_review_specialist_pin is None
    assert loaded.video_understanding is None
    assert loaded.efficiency is not None
    assert loaded.efficiency.direct_resolve_minutes is None
    assert loaded.pass_policy.require_efficiency_timing is False
    assert loaded.evaluation_binding is None


def test_efficiency_timing_nulls_block_pass_policy() -> None:
    anchors = (_anchor("k-001", 0, 100, "must_keep"),)
    m = compute_editorial_metrics(anchors, [(0, 100)])

    def _report(efficiency: EfficiencyMetrics | None) -> ProductProofReportV1:
        return ProductProofReportV1(
            run=RunIdentity(
                episode_id="test-ep-t8-gate",
                commit_sha="a" * 40,
                run_kind="v44-0",
                model_pin="m",
                analysis_provider_pin="a",
            ),  # type: ignore[arg-type]
            editorial=m,
            operator=OperatorVerdict(continuation_yes_no=True, publishability="as_is"),
            efficiency=efficiency,
            pass_policy=PassPolicyBlock(require_efficiency_timing=True),
        )

    partial = _report(
        EfficiencyMetrics(
            wall_clock_seconds=100.0, aht_minutes=5.0, direct_resolve_minutes=2.0
        )
    )
    result = evaluate_pass_policy(partial)
    assert result.passed is False
    assert "efficiency_ttfrp_seconds" in result.pending_criteria

    none_at_all = _report(None)
    result_none = evaluate_pass_policy(none_at_all)
    assert result_none.passed is False
    assert {
        "efficiency_wall_clock_seconds",
        "efficiency_aht_minutes",
        "efficiency_direct_resolve_minutes",
        "efficiency_ttfrp_seconds",
    } <= set(result_none.pending_criteria)

    full = _report(
        EfficiencyMetrics(
            wall_clock_seconds=100.0,
            aht_minutes=5.0,
            direct_resolve_minutes=2.0,
            ttfrp_seconds=600.0,
        )
    )
    result_full = evaluate_pass_policy(full)
    assert result_full.passed is True
    assert not [c for c in result_full.pending_criteria if c.startswith("efficiency_")]


def test_summarize_time_log_five_categories_once_and_direct_subset() -> None:
    rows: tuple[tuple[TimeLogPhase, float], ...] = (
        ("ordinary_review", 12.5),
        ("kit_bootstrap", 3.5),
        ("taste_calibration", 1.5),
        ("troubleshooting", 2.5),
        ("direct_resolve", 40.0),
    )
    lines = tuple(
        TimeLogLineV1(
            schema_version="v44-time-log-v1",
            phase=phase,
            minutes=minutes,
            label="bootstrap",
            at="2026-08-30T00:00:00Z",
        )
        for phase, minutes in rows
    )
    totals = summarize_time_log(lines)
    # AHT counts each of the five categories exactly once (60.0, not 100.0
    # which would double-count direct Resolve).
    assert totals.aht_minutes == 60.0
    # Direct Resolve is reported as a subset, never added a second time.
    assert totals.direct_resolve_minutes == 40.0
    assert set(totals.phase_minutes) == {phase for phase, _ in rows}
    assert totals.phase_minutes["ordinary_review"] == 12.5


def test_summarize_time_log_empty_stays_null() -> None:
    totals = summarize_time_log(())
    assert totals.aht_minutes is None
    assert totals.direct_resolve_minutes is None
    assert dict(totals.phase_minutes) == {}


def test_video_understanding_metrics_dedup_stage_costs_and_coverage() -> None:
    # Two per-window reviews REPLICATE the shared reduce stage and the
    # overlapping specialist stage (T6 lineage design); each review's
    # primary lineage.cost repeats its fusion stage cost.
    r1 = _fused_review(
        "test-ep-t8-vu",
        0,
        100,
        (
            _vu_stage("local_map", 0, 100, pin=_LEAD_PIN, input_sha="l1", cost=1.0),
            _vu_stage("global_reduce", 0, 200, pin=_LEAD_PIN, input_sha="R", cost=2.0),
            _vu_stage("specialist", 50, 150, pin=_SPEC_PIN, input_sha="s1", cost=3.0),
            _vu_stage(
                "fusion", 0, 100, pin=_LEAD_PIN, input_sha="f1", cost=0.5, attempts=2
            ),
        ),
        primary_cost=0.5,
        duration=200,
    )
    r2 = _fused_review(
        "test-ep-t8-vu",
        100,
        200,
        (
            _vu_stage("local_map", 100, 200, pin=_LEAD_PIN, input_sha="l2", cost=1.0),
            _vu_stage("global_reduce", 0, 200, pin=_LEAD_PIN, input_sha="R", cost=2.0),
            _vu_stage("specialist", 50, 150, pin=_SPEC_PIN, input_sha="s1", cost=3.0),
            _vu_stage("fusion", 100, 200, pin=_LEAD_PIN, input_sha="f2", cost=0.5),
        ),
        primary_cost=0.5,
        duration=200,
    )
    metrics = compute_video_understanding_metrics((r1, r2))
    assert metrics is not None
    assert metrics.local_window_count == 2
    assert metrics.source_coverage == 1.0
    assert metrics.targeted_unique_coverage == 0.5
    assert metrics.specialist_target_count == 1
    assert metrics.specialist_success_count == 1
    assert metrics.specialist_failure_count == 0
    assert metrics.retry_count == 1
    assert metrics.unresolved_count == 0
    # Each real provider call counted exactly once: the replicated reduce
    # (2.0, not 4.0) and replicated specialist (3.0, not 6.0) collapse; the
    # primary fused-review costs (0.5 x 2) are NOT added on top.
    assert metrics.lead_cost == 4.0
    assert metrics.specialist_cost == 3.0
    assert metrics.fusion_cost == 1.0
    assert metrics.total_cost == 8.0
    assert metrics.stage_pin_sha256 == tuple(sorted((_LEAD_PIN, _SPEC_PIN)))


def test_video_understanding_metrics_failed_specialist_unresolved_and_null_cost() -> None:
    review = _fused_review(
        "test-ep-t8-vu2",
        0,
        100,
        (
            _vu_stage("local_map", 0, 100, pin=_LEAD_PIN, input_sha="l1", cost=1.0),
            _vu_stage("global_reduce", 0, 100, pin=_LEAD_PIN, input_sha="R", cost=2.0),
            _vu_stage("specialist", 0, 40, pin=_SPEC_PIN, input_sha="s1", cost=0.3),
            _vu_stage("specialist", 60, 80, pin=_SPEC_PIN, input_sha="s2", cost=None, ok=False),
            _vu_stage("fusion", 0, 100, pin=_LEAD_PIN, input_sha="f1", cost=0.5),
        ),
        primary_cost=0.5,
        duration=100,
    )
    metrics = compute_video_understanding_metrics((review,))
    assert metrics is not None
    assert metrics.specialist_target_count == 2
    assert metrics.specialist_success_count == 1
    assert metrics.specialist_failure_count == 1
    assert metrics.unresolved_count == 1
    # A partial cost ledger under-reports: the total stays null, never a
    # silent partial sum.
    assert metrics.specialist_cost is None
    assert metrics.total_cost is None
    assert metrics.lead_cost == 3.0
    assert metrics.fusion_cost == 0.5
    assert metrics.targeted_unique_coverage == 0.6  # (40 + 20) / 100


def test_video_understanding_metrics_rejects_inconsistent_counts() -> None:
    with pytest.raises(ValidationError, match="specialist_counts_mismatch"):
        VideoUnderstandingMetrics(
            local_window_count=1,
            specialist_target_count=2,
            specialist_success_count=2,
            specialist_failure_count=1,
            retry_count=0,
            unresolved_count=1,
        )


def test_compute_video_understanding_metrics_none_without_reviews() -> None:
    assert compute_video_understanding_metrics(()) is None


def test_run_arm_b_carries_pins_and_video_understanding_block() -> None:
    gt = EditorialGroundTruthV1(
        episode_id="test-ep-t8-arm",
        anchors=(_anchor("a-001", 0, 100, "must_keep"),),
        created_at="2026-08-23T00:00:00+00:00",
        operator="op",
    )
    ctx = EpisodeContext(
        episode_id="test-ep-t8-arm", ground_truth=gt, kept_spans=((0, 100),)
    )
    review = _fused_review(
        "test-ep-t8-arm",
        0,
        100,
        (
            _vu_stage("local_map", 0, 100, pin=_LEAD_PIN, input_sha="l1", cost=1.0),
            _vu_stage("global_reduce", 0, 100, pin=_LEAD_PIN, input_sha="R", cost=2.0),
            _vu_stage("fusion", 0, 100, pin=_LEAD_PIN, input_sha="f1", cost=0.5),
        ),
        primary_cost=0.5,
        duration=100,
    )
    result = run_arm_b(
        ctx,
        (review,),
        moment_review_lead_pin="gemini-3.7-flash",
        moment_review_specialist_pin="glm-5v-turbo",
    )
    assert result.report.run.moment_review_lead_pin == "gemini-3.7-flash"
    assert result.report.run.moment_review_specialist_pin == "glm-5v-turbo"
    block = result.report.video_understanding
    assert block is not None
    assert block.local_window_count == 1
    assert block.source_coverage == 1.0


def _finishing_report(episode_id: str, wall_clock: float) -> FinishingRunReportV1:
    return FinishingRunReportV1(
        schema_version="v44-finishing-run-v1",
        episode_id=episode_id,  # type: ignore[arg-type]
        run_id="run-t8",
        executor="fake",
        executor_note="test harness",
        director_model_id="gpt-5.6-sol",
        analysis_provider="whisper-cpp-cli:abc123",
        review_head_version=1,
        kit_selections=(),
        plan_compiled=False,
        domain_statuses=dict.fromkeys(QUALITY_DOMAIN_NAMES, "applied"),
        domain_justifications={},
        blocked_domains=(),
        surfaced_manual_items=(),
        gate_decision="pass",
        technical_qc=FinishingQcBlockV1(reason="qc not run in test"),
        editorial_qc=FinishingEditorialQcBlockV1(
            candidate_count=1, critical_count=0, needs_review_count=0, report_path="e.json"
        ),
        final_preview_path="preview.mp4",
        final_preview_sha256="f" * 64,
        wall_clock_seconds=wall_clock,
    )


def _observation(
    ttfrp: float | None, episode_id: str = "test-ep-t8-eff"
) -> V1ObservationRecord:
    return V1ObservationRecord(
        episode_id=episode_id,  # type: ignore[arg-type]
        stage_timeline=ObservationStageTimelineV1(
            job_status="PREVIEW_READY",
            current_stage="preview",
            runs=(),
        ),
        ttfrp_seconds=ttfrp,
        corrections=(),
        rebuild_records=(),
        internal_path_leak=False,
        observed_at="2026-08-30T00:00:00Z",
    )


def _write_t8_episode(tmp_path: Path, *, observation: bool) -> Path:
    episode_root = tmp_path / "ep-t8"
    finishing_dir = episode_root / "finishing"
    finishing_dir.mkdir(parents=True)
    atomic_write_finishing = finishing_dir / "finishing-run.json"
    atomic_write_finishing.write_bytes(
        canonical_model_bytes(_finishing_report("test-ep-t8-eff", 2903.44))
    )
    rows: tuple[tuple[TimeLogPhase, float], ...] = (
        ("ordinary_review", 12.5),
        ("kit_bootstrap", 3.5),
        ("taste_calibration", 1.5),
        ("troubleshooting", 2.5),
        ("direct_resolve", 40.0),
    )
    log = episode_root / "time-log.jsonl"
    with log.open("ab") as stream:
        for phase, minutes in rows:
            line = TimeLogLineV1(
                schema_version="v44-time-log-v1",
                phase=phase,
                minutes=minutes,
                label="bootstrap",
                at="2026-08-30T00:00:00Z",
            )
            stream.write(line.model_dump_json().encode() + b"\n")
    if observation:
        (episode_root / "observation.json").write_bytes(
            canonical_model_bytes(_observation(612.0))
        )
    return episode_root


def _write_t8_report(tmp_path: Path) -> Path:
    gt = EditorialGroundTruthV1(
        episode_id="test-ep-t8-eff",
        anchors=(_anchor("k-001", 0, 100, "must_keep"),),
        created_at="2026-08-23T00:00:00+00:00",
        operator="op",
    )
    ctx = EpisodeContext(
        episode_id="test-ep-t8-eff", ground_truth=gt, kept_spans=((0, 100),)
    )
    report = run_arm_a(ctx).report
    report_path = tmp_path / "report-t8.json"
    report_path.write_bytes(canonical_model_bytes(report))
    return report_path


def test_record_efficiency_carries_finishing_wall_clock_and_time_log(
    tmp_path: Path,
) -> None:
    episode_root = _write_t8_episode(tmp_path, observation=True)
    report_path = _write_t8_report(tmp_path)
    rc = cli_main(
        [
            "record-efficiency",
            "--report",
            str(report_path),
            "--episode-root",
            str(episode_root),
        ]
    )
    assert rc == 0
    loaded = ProductProofReportV1.model_validate(
        json.loads(report_path.read_text(encoding="utf-8"))
    )
    eff = loaded.efficiency
    assert eff is not None
    assert eff.wall_clock_seconds == 2903.44  # carried from the finishing report
    assert eff.aht_minutes == 60.0
    assert eff.direct_resolve_minutes == 40.0
    assert eff.ttfrp_seconds == 612.0
    assert loaded.pass_policy.require_efficiency_timing is True
    policy = evaluate_pass_policy(loaded)
    assert not [c for c in policy.pending_criteria if c.startswith("efficiency_")]
    rc2 = cli_main(
        [
            "record-operator-verdict",
            "--report",
            str(report_path),
            "--continuation",
            "yes",
            "--publishability",
            "as_is",
        ]
    )
    assert rc2 == 0
    loaded2 = ProductProofReportV1.model_validate(
        json.loads(report_path.read_text(encoding="utf-8"))
    )
    assert evaluate_pass_policy(loaded2).passed is True


def test_record_efficiency_missing_ttfrp_stays_null_and_blocks_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    episode_root = _write_t8_episode(tmp_path, observation=False)
    report_path = _write_t8_report(tmp_path)
    rc = cli_main(
        [
            "record-efficiency",
            "--report",
            str(report_path),
            "--episode-root",
            str(episode_root),
        ]
    )
    assert rc == 0  # honest nulls recorded, not a refusal and not an estimate
    loaded = ProductProofReportV1.model_validate(
        json.loads(report_path.read_text(encoding="utf-8"))
    )
    assert loaded.efficiency is not None
    assert loaded.efficiency.ttfrp_seconds is None
    policy = evaluate_pass_policy(loaded)
    assert policy.passed is False
    assert "efficiency_ttfrp_seconds" in policy.pending_criteria
    rc2 = cli_main(
        [
            "record-operator-verdict",
            "--report",
            str(report_path),
            "--continuation",
            "yes",
            "--publishability",
            "as_is",
        ]
    )
    assert rc2 == 0
    loaded2 = ProductProofReportV1.model_validate(
        json.loads(report_path.read_text(encoding="utf-8"))
    )
    final = evaluate_pass_policy(loaded2)
    assert final.passed is False  # the gate still cannot pass on null TTFRP
    assert "efficiency_ttfrp_seconds" in final.pending_criteria
    captured = capsys.readouterr()
    assert "time to first" in captured.out + captured.err


def test_record_efficiency_refuses_without_finishing_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report_path = _write_t8_report(tmp_path)
    empty_root = tmp_path / "ep-empty"
    empty_root.mkdir()
    rc = cli_main(
        [
            "record-efficiency",
            "--report",
            str(report_path),
            "--episode-root",
            str(empty_root),
        ]
    )
    assert rc == 2
    assert "finishing-report-missing" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# T8 repair — parent-verified truth gaps (red-first regressions)
# ---------------------------------------------------------------------------


def test_summarize_time_log_partial_phases_keep_aht_null() -> None:
    # Four of the five canonical phases recorded (taste_calibration absent):
    # a partial log must NOT produce an AHT total.
    rows: tuple[tuple[TimeLogPhase, float], ...] = (
        ("ordinary_review", 12.5),
        ("kit_bootstrap", 3.5),
        ("troubleshooting", 2.5),
        ("direct_resolve", 40.0),
    )
    lines = tuple(
        TimeLogLineV1(
            schema_version="v44-time-log-v1",
            phase=phase,
            minutes=minutes,
            label="bootstrap",
            at="2026-08-30T00:00:00Z",
        )
        for phase, minutes in rows
    )
    totals = summarize_time_log(lines)
    assert totals.aht_minutes is None
    # The recorded direct-Resolve subset stays truthful even when AHT is null.
    assert totals.direct_resolve_minutes == 40.0
    assert "taste_calibration" not in totals.phase_minutes


def test_final_consolidated_report_cannot_bypass_timing_gate() -> None:
    anchors = (_anchor("k-001", 0, 100, "must_keep"),)
    m = compute_editorial_metrics(anchors, [(0, 100)])

    def _report(run_kind: str, *, require_timing: bool) -> ProductProofReportV1:
        return ProductProofReportV1(
            run=RunIdentity(
                episode_id="test-ep-t8-final",
                commit_sha="a" * 40,
                run_kind=run_kind,  # type: ignore[arg-type]
                model_pin="m",
                analysis_provider_pin="a",
            ),
            editorial=m,
            operator=OperatorVerdict(continuation_yes_no=True, publishability="as_is"),
            efficiency=None,
            pass_policy=PassPolicyBlock(require_efficiency_timing=require_timing),
        )

    final = _report("v44-consolidated", require_timing=False)
    result = evaluate_pass_policy(final)
    assert result.passed is False
    assert {
        "efficiency_wall_clock_seconds",
        "efficiency_aht_minutes",
        "efficiency_direct_resolve_minutes",
        "efficiency_ttfrp_seconds",
    } <= set(result.pending_criteria)

    # Non-final kinds keep the historical optional-timing semantics unless
    # their policy explicitly requires timing.
    arm_default = _report("v44-0-arm", require_timing=False)
    assert evaluate_pass_policy(arm_default).passed is True
    consolidated_default = _report("v44-0", require_timing=False)
    assert evaluate_pass_policy(consolidated_default).passed is True
    arm_explicit = _report("v44-0-arm", require_timing=True)
    assert evaluate_pass_policy(arm_explicit).passed is False


def test_video_understanding_metrics_trailing_gap_uses_reduce_denominator() -> None:
    # The global reduce validated [0, 200) as the full episode interval, but
    # the local maps only cover [0, 100): coverage is 0.5, never 1.0 from a
    # denominator shrunk to the local windows.
    review = _fused_review(
        "test-ep-t8-gap",
        0,
        100,
        (
            _vu_stage("local_map", 0, 100, pin=_LEAD_PIN, input_sha="l1", cost=1.0),
            _vu_stage("global_reduce", 0, 200, pin=_LEAD_PIN, input_sha="R", cost=2.0),
            _vu_stage("fusion", 0, 100, pin=_LEAD_PIN, input_sha="f1", cost=0.5),
        ),
        primary_cost=0.5,
        duration=200,
    )
    metrics = compute_video_understanding_metrics((review,))
    assert metrics is not None
    assert metrics.local_window_count == 1
    assert metrics.source_coverage == 0.5
    assert metrics.targeted_unique_coverage == 0.0


def test_video_understanding_metrics_no_reduce_truth_leaves_coverage_null() -> None:
    # Without a validated global-reduce interval there is no trustworthy
    # denominator: coverage stays null instead of being inferred from the
    # (possibly incomplete) local windows.
    review = _fused_review(
        "test-ep-t8-noreduce",
        0,
        100,
        (
            _vu_stage("local_map", 0, 100, pin=_LEAD_PIN, input_sha="l1", cost=1.0),
            _vu_stage("fusion", 0, 100, pin=_LEAD_PIN, input_sha="f1", cost=0.5),
        ),
        primary_cost=0.5,
        duration=100,
    )
    metrics = compute_video_understanding_metrics((review,))
    assert metrics is not None
    assert metrics.source_coverage is None
    assert metrics.targeted_unique_coverage is None
    assert metrics.local_window_count == 1
    assert metrics.lead_cost == 1.0
    assert metrics.fusion_cost == 0.5


def test_video_understanding_metrics_pin_change_counts_distinct_calls() -> None:
    # Same purpose/range/input under two different pins = two real provider
    # calls (a re-pin must not collapse spend), while replicated identical
    # lineage with the SAME pin stays counted once.
    lead_after_repin = "lead-pin-sha-v2"
    r1 = _fused_review(
        "test-ep-t8-repin",
        0,
        100,
        (
            _vu_stage("local_map", 0, 100, pin=_LEAD_PIN, input_sha="l1", cost=1.0),
            _vu_stage("global_reduce", 0, 100, pin=_LEAD_PIN, input_sha="R", cost=2.0),
            _vu_stage("fusion", 0, 100, pin=_LEAD_PIN, input_sha="f1", cost=0.5),
        ),
        primary_cost=0.5,
        duration=100,
    )
    r2 = _fused_review(
        "test-ep-t8-repin",
        100,
        200,
        (
            # Same purpose/range/input hash as r1's local, DIFFERENT pin: a
            # re-pin is a distinct real call and must both count.
            _vu_stage("local_map", 0, 100, pin=lead_after_repin, input_sha="l1", cost=1.5),
            # Replicated identical reduce lineage (same pin): still ONE call.
            _vu_stage("global_reduce", 0, 100, pin=_LEAD_PIN, input_sha="R", cost=2.0),
            _vu_stage("fusion", 100, 200, pin=lead_after_repin, input_sha="f2", cost=0.7),
        ),
        primary_cost=0.7,
        duration=200,
    )
    metrics = compute_video_understanding_metrics((r1, r2))
    assert metrics is not None
    assert metrics.local_window_count == 2
    # lead = local(1.0) + local-after-repin(1.5) + reduce counted once (2.0).
    assert metrics.lead_cost == 4.5
    assert metrics.fusion_cost == 1.2
    assert metrics.stage_pin_sha256 == tuple(
        sorted((_LEAD_PIN, lead_after_repin))
    )


def test_record_efficiency_refuses_observation_from_another_episode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    episode_root = _write_t8_episode(tmp_path, observation=True)
    # Overwrite the observation with another episode's record.
    (episode_root / "observation.json").write_bytes(
        canonical_model_bytes(_observation(999.0, episode_id="other-ep-xyz"))
    )
    report_path = _write_t8_report(tmp_path)
    before = report_path.read_bytes()
    rc = cli_main(
        [
            "record-efficiency",
            "--report",
            str(report_path),
            "--episode-root",
            str(episode_root),
        ]
    )
    assert rc == 2
    assert "observation-episode-mismatch" in capsys.readouterr().err
    # Typed refusal happens BEFORE the report rewrite: bytes unchanged.
    assert report_path.read_bytes() == before


# ---------------------------------------------------------------------------
# T8 repair round 2 — production consolidation kind + failed-reduce truth
# ---------------------------------------------------------------------------


def test_build_progressive_report_emits_final_run_kind_and_gates_timing(
    tmp_path: Path,
) -> None:
    # The PRODUCTION consolidation path: both arm reports come from the
    # existing builders, the operator verdict rides the CLI seam, then the
    # consolidated report must carry the final run kind and cannot pass
    # policy on missing timing even though the policy flag is False.
    gt = EditorialGroundTruthV1(
        episode_id="test-ep-t8-consolidated",
        anchors=(
            _anchor("a-001", 0, 100, "must_keep"),
            _anchor("a-002", 200, 300, "must_keep"),
        ),
        created_at="2026-08-23T00:00:00+00:00",
        operator="op",
    )
    ctx = EpisodeContext(
        episode_id="test-ep-t8-consolidated",
        ground_truth=gt,
        kept_spans=((0, 100), (200, 300)),
    )
    binding = _eval_binding()
    report_a = run_arm_a(ctx, evaluation_binding=binding).report
    review = _fused_review(
        "test-ep-t8-consolidated",
        0,
        100,
        (
            _vu_stage("local_map", 0, 100, pin=_LEAD_PIN, input_sha="l1", cost=1.0),
            _vu_stage("global_reduce", 0, 300, pin=_LEAD_PIN, input_sha="R", cost=2.0),
            _vu_stage("fusion", 0, 100, pin=_LEAD_PIN, input_sha="f1", cost=0.5),
        ),
        primary_cost=0.5,
        duration=300,
    )
    report_b = run_arm_b(ctx, (review,), evaluation_binding=binding).report
    assert report_b.pass_policy.require_efficiency_timing is False

    b_path = tmp_path / "report-b.json"
    b_path.write_bytes(canonical_model_bytes(report_b))
    rc = cli_main(
        [
            "record-operator-verdict",
            "--report",
            str(b_path),
            "--continuation",
            "yes",
            "--publishability",
            "as_is",
        ]
    )
    assert rc == 0
    loaded_b = ProductProofReportV1.model_validate(
        json.loads(b_path.read_text(encoding="utf-8"))
    )

    consolidated = build_progressive_report(report_a, loaded_b)
    assert consolidated.run.run_kind == "v44-consolidated"
    assert consolidated.pass_policy.require_efficiency_timing is False
    assert consolidated.video_understanding is not None

    result = evaluate_pass_policy(consolidated)
    assert result.passed is False
    # Good editorial/operator data isolates timing as the only blocker.
    assert set(result.pending_criteria) == {
        "efficiency_wall_clock_seconds",
        "efficiency_aht_minutes",
        "efficiency_direct_resolve_minutes",
        "efficiency_ttfrp_seconds",
    }

    # The same consolidated report passes once truthful timing is recorded.
    timing_recorded = ProductProofReportV1(
        run=consolidated.run,
        evaluation_binding=consolidated.evaluation_binding,
        editorial=consolidated.editorial,
        progressive_lift=consolidated.progressive_lift,
        evidence_quality=consolidated.evidence_quality,
        video_understanding=consolidated.video_understanding,
        operator=consolidated.operator,
        efficiency=EfficiencyMetrics(
            wall_clock_seconds=2903.44,
            aht_minutes=60.0,
            direct_resolve_minutes=40.0,
            ttfrp_seconds=612.0,
            provider_cost=None,
        ),
        pass_policy=consolidated.pass_policy,
        notes=consolidated.notes,
    )
    final = evaluate_pass_policy(timing_recorded)
    assert final.passed is True
    assert final.pending_criteria == ()


def test_video_understanding_metrics_failed_reduce_cannot_set_denominator() -> None:
    # A failed global-reduce request carries NO analyzed interval: it must
    # not establish the coverage denominator, no matter how wide its
    # requested bounds were.
    review = _fused_review(
        "test-ep-t8-failedreduce",
        0,
        100,
        (
            _vu_stage("local_map", 0, 100, pin=_LEAD_PIN, input_sha="l1", cost=1.0),
            _vu_stage("global_reduce", 0, 200, pin=_LEAD_PIN, input_sha="R", ok=False),
            _vu_stage("fusion", 0, 100, pin=_LEAD_PIN, input_sha="f1", cost=0.5),
        ),
        primary_cost=0.5,
        duration=200,
    )
    metrics = compute_video_understanding_metrics((review,))
    assert metrics is not None
    assert metrics.source_coverage is None
    assert metrics.targeted_unique_coverage is None
    assert metrics.local_window_count == 1


def test_evaluation_binding_rejects_malformed_fields() -> None:
    # Given/When/Then: each malformed field (unknown lane, non-hex GT hash,
    # uppercase transcript hash, empty label) must be a typed parse refusal.
    with pytest.raises(ValidationError, match="evidence_lane"):
        _eval_binding(lane="operator_corrected")
    with pytest.raises(ValidationError, match="ground_truth_sha256"):
        _eval_binding(gt_sha="not-hex")
    with pytest.raises(ValidationError, match="transcript_sha256"):
        _eval_binding(transcript_sha="D" * 64)
    with pytest.raises(ValidationError, match="ground_truth_label"):
        _eval_binding(label="")


def _bound_ctx() -> EpisodeContext:
    gt = EditorialGroundTruthV1(
        episode_id="test-ep-binding",
        anchors=(_anchor("a-001", 0, 100, "must_keep"),),
        created_at="2026-08-30T00:00:00+00:00",
        operator="op",
    )
    return EpisodeContext(
        episode_id="test-ep-binding",
        ground_truth=gt,
        kept_spans=((0, 100),),
    )


def test_build_progressive_report_refuses_missing_evaluation_bindings() -> None:
    ctx = _bound_ctx()
    report_a = run_arm_a(ctx).report
    report_b = run_arm_a(ctx).report

    with pytest.raises(EvaluationBindingError) as both_missing:
        build_progressive_report(report_a, report_b)
    assert both_missing.value.code == "evaluation-binding-mismatch"
    assert "A" in both_missing.value.detail
    assert "B" in both_missing.value.detail

    bound_a = run_arm_a(ctx, evaluation_binding=_eval_binding()).report
    with pytest.raises(EvaluationBindingError) as b_missing:
        build_progressive_report(bound_a, report_b)
    assert b_missing.value.code == "evaluation-binding-mismatch"
    assert "B" in b_missing.value.detail


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("ground_truth_sha256", {"gt_sha": "e" * 64}),
        ("ground_truth_label", {"label": "v1"}),
        ("evidence_lane", {"lane": "system_asr"}),
        ("transcript_sha256", {"transcript_sha": "f" * 64}),
    ],
)
def test_build_progressive_report_refuses_unequal_evaluation_bindings(
    field: str, kwargs: dict[str, str]
) -> None:
    ctx = _bound_ctx()
    report_a = run_arm_a(ctx, evaluation_binding=_eval_binding()).report
    report_b = run_arm_a(ctx, evaluation_binding=_eval_binding(**kwargs)).report

    with pytest.raises(EvaluationBindingError) as mismatch:
        build_progressive_report(report_a, report_b)
    assert mismatch.value.code == "evaluation-binding-mismatch"
    assert field in mismatch.value.detail


def test_build_progressive_report_carries_equal_binding() -> None:
    ctx = _bound_ctx()
    binding = _eval_binding()
    report_a = run_arm_a(ctx, evaluation_binding=binding).report
    report_b = run_arm_a(ctx, evaluation_binding=binding).report

    consolidated = build_progressive_report(report_a, report_b)
    assert consolidated.run.run_kind == "v44-consolidated"
    assert consolidated.evaluation_binding == binding
