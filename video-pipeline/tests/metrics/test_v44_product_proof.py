from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.cli.v44_product_proof import main as cli_main
from services.foundation_io import canonical_model_bytes
from services.media_intelligence.moment_review import (
    AudioContext,
    BestSubSpan,
    FrameBundleEntry,
    MomentAssessment,
    ReviewConfidence,
    ReviewEvidence,
    ReviewLineage,
    ReviewRecordRequest,
    ReviewWindow,
    record_review,
)
from services.media_intelligence.moment_review_real import (
    SyntheticLineageError,
    require_real_lineage,
)
from services.metrics.v44_product_proof import (
    AnchorLabel,
    EditorialGroundTruthV1,
    EpisodeContext,
    GroundTruthAnchor,
    OperatorVerdict,
    ProductProofReportV1,
    RunIdentity,
    TranscriptSampleV1,
    TranscriptSegment,
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


def _anchor(
    anchor_id: str, s: int, e: int, label: AnchorLabel, note: str | None = None
) -> GroundTruthAnchor:
    return GroundTruthAnchor(
        anchor_id=anchor_id, start_frame=s, end_frame=e, label=label, note=note
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
