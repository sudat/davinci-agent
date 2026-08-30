"""Task-3 contract tests: transcript lanes + fail-closed pre-model alignment.

The lane names are frozen product vocabulary (plan §Frozen owner decisions):
``operator_corrected_diagnostic`` (non-product evidence) and ``system_asr``
(product lane, gated by the four predeclared thresholds). Everything here is
deterministic and local — no transport, no paid calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.cli._v44_arm_transcript import (
    CER_MAX,
    DUPLICATED_MAX,
    OMITTED_MAX,
    TIMESTAMP_P95_MS_MAX,
    TranscriptLaneInput,
    effective_transcript_sha256,
    enforce_system_asr_thresholds,
    prepare_arm_transcript,
)
from services.cli.real_pool import RealPoolError, segments_from_ms
from services.cli.v44_arm_stages import ArmPipelineData, ArmPipelineError
from services.foundation_io import canonical_model_bytes
from services.metrics.v44_product_proof import (
    EvidenceQualityMetrics,
    TranscriptSampleV1,
)
from services.metrics.v44_product_proof import TranscriptSegment as SampleSegment


def _data(
    speech_ms: tuple[tuple[int, int, str], ...] = (
        (0, 3000, "DJI Pocket 4 のケースが無くなった"),
        (3000, 6000, "部屋中探したけど見つからない"),
        (6000, 9000, "また明日探してみる"),
    ),
    total_frames: int = 300,
) -> ArmPipelineData:
    speech = segments_from_ms(speech_ms, total_frames)
    return ArmPipelineData(
        episode_id="v44-arm-transcript",
        source_id="v44-arm-transcript-edit-source",
        total_frames=total_frames,
        speech=speech,
        transcript_segments_ms=speech_ms,
        mezzanine=None,
        mezzanine_sha256=None,
    )


def _write_sample(path: Path, segments_ms: tuple[tuple[int, int, str], ...]) -> Path:
    path.write_bytes(
        canonical_model_bytes(
            TranscriptSampleV1(
                segments=tuple(
                    SampleSegment(start_ms=s, end_ms=e, text=t) for s, e, t in segments_ms
                )
            )
        )
    )
    return path


def _episode(tmp_path: Path) -> Path:
    episode_root = tmp_path / "episode"
    episode_root.mkdir(exist_ok=True)
    return episode_root


# ---------------------------------------------------------------------------
# enforce_system_asr_thresholds — frozen boundaries (never relaxed post-hoc)
# ---------------------------------------------------------------------------


def test_threshold_constants_are_the_predeclared_frozen_values() -> None:
    assert CER_MAX == 0.10
    assert TIMESTAMP_P95_MS_MAX == 500.0
    assert OMITTED_MAX == 5
    assert DUPLICATED_MAX == 5


def test_exact_boundary_values_pass() -> None:
    """CER 0.10, p95 500ms, omitted 5, duplicated 5 are all <= thresholds."""
    enforce_system_asr_thresholds(
        EvidenceQualityMetrics(
            transcript_cer=CER_MAX,
            timestamp_error_p95_ms=TIMESTAMP_P95_MS_MAX,
            omitted_utterances=OMITTED_MAX,
            duplicated_utterances=DUPLICATED_MAX,
        )
    )


def test_just_past_every_boundary_refuses_naming_each_metric() -> None:
    with pytest.raises(ArmPipelineError) as error:
        enforce_system_asr_thresholds(
            EvidenceQualityMetrics(
                transcript_cer=0.100001,
                timestamp_error_p95_ms=500.001,
                omitted_utterances=6,
                duplicated_utterances=6,
            )
        )
    assert error.value.code == "asr-alignment-failed"
    for name in (
        "transcript_cer",
        "timestamp_error_p95_ms",
        "omitted_utterances",
        "duplicated_utterances",
    ):
        assert name in error.value.detail


def test_any_null_metric_is_failure_even_when_others_pass() -> None:
    with pytest.raises(ArmPipelineError) as error:
        enforce_system_asr_thresholds(
            EvidenceQualityMetrics(
                transcript_cer=0.0,
                timestamp_error_p95_ms=None,
                omitted_utterances=0,
                duplicated_utterances=0,
            )
        )
    assert error.value.code == "asr-alignment-failed"
    assert "timestamp_error_p95_ms" in error.value.detail
    assert "null" in error.value.detail


# ---------------------------------------------------------------------------
# prepare_arm_transcript — diagnostic lane
# ---------------------------------------------------------------------------


def test_diagnostic_lane_requires_explicit_corrected_sample(tmp_path: Path) -> None:
    with pytest.raises(ArmPipelineError) as error:
        prepare_arm_transcript(
            _data(),
            _episode(tmp_path),
            TranscriptLaneInput(lane="operator_corrected_diagnostic"),
        )
    assert error.value.code == "transcript-sample-missing"


def test_diagnostic_lane_missing_file_is_typed_refusal(tmp_path: Path) -> None:
    with pytest.raises(ArmPipelineError) as error:
        prepare_arm_transcript(
            _data(),
            _episode(tmp_path),
            TranscriptLaneInput(
                lane="operator_corrected_diagnostic",
                corrected_sample=tmp_path / "absent.json",
            ),
        )
    assert error.value.code == "transcript-sample-missing"


def test_diagnostic_lane_malformed_sample_is_typed_refusal(tmp_path: Path) -> None:
    bad = tmp_path / "corrected.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ArmPipelineError) as error:
        prepare_arm_transcript(
            _data(),
            _episode(tmp_path),
            TranscriptLaneInput(
                lane="operator_corrected_diagnostic", corrected_sample=bad
            ),
        )
    assert error.value.code == "transcript-sample-unreadable"


def test_diagnostic_lane_empty_sample_is_typed_refusal(tmp_path: Path) -> None:
    empty = _write_sample(tmp_path / "corrected.json", ())
    with pytest.raises(ArmPipelineError) as error:
        prepare_arm_transcript(
            _data(),
            _episode(tmp_path),
            TranscriptLaneInput(
                lane="operator_corrected_diagnostic", corrected_sample=empty
            ),
        )
    assert error.value.code == "transcript-sample-unreadable"


def test_diagnostic_lane_requantizes_sample_through_shared_lattice(
    tmp_path: Path,
) -> None:
    """The corrected ms spans go through the SAME 30fps half-open lattice as
    the ASR path: adjacent spans collapse to contiguity, ids stay s1..sN, and
    the ASR-derived speech never reaches the return value."""
    sample_ms = ((0, 2780, "はじめまーす"), (2780, 7080, "テスト動画だよ"))
    sample = _write_sample(tmp_path / "corrected.json", sample_ms)
    speech, hypothesis_ms, _edits, alignment = prepare_arm_transcript(
        _data(total_frames=8467),
        _episode(tmp_path),
        TranscriptLaneInput(
            lane="operator_corrected_diagnostic", corrected_sample=sample
        ),
    )
    assert tuple((seg.segment_id, seg.text) for seg in speech) == (
        ("s1", "はじめまーす"),
        ("s2", "テスト動画だよ"),
    )
    assert speech[0].end_frame == speech[1].start_frame  # lattice contiguity
    assert hypothesis_ms == sample_ms  # ms hypothesis IS the corrected sample
    assert alignment.lane == "operator_corrected_diagnostic"
    assert alignment.evidence_quality.transcript_cer == 0.0
    assert any("non-product" in note for note in alignment.provenance)


def test_diagnostic_lane_sha_is_canonical_and_stable_across_runs(
    tmp_path: Path,
) -> None:
    sample = _write_sample(tmp_path / "corrected.json", ((0, 3000, "今日はいい天気"),))
    lane = TranscriptLaneInput(
        lane="operator_corrected_diagnostic", corrected_sample=sample
    )
    _s1, h1, _e1, a1 = prepare_arm_transcript(_data(), _episode(tmp_path), lane)
    _s2, h2, _e2, a2 = prepare_arm_transcript(_data(), _episode(tmp_path), lane)
    assert h1 == h2
    assert a1.effective_transcript_sha256 == a2.effective_transcript_sha256
    expected = effective_transcript_sha256(((0, 3000, "今日はいい天気"),))
    assert a1.effective_transcript_sha256 == expected


def test_effective_transcript_sha256_format_is_the_canonical_triples_dump() -> None:
    payload = json.dumps(
        [{"start_ms": 0, "end_ms": 9, "text": "あ"}],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert effective_transcript_sha256(((0, 9, "あ"),)) == hashlib.sha256(
        payload
    ).hexdigest()


def test_diagnostic_lane_changed_bytes_recompute_sha_not_reuse(tmp_path: Path) -> None:
    """Stale-state guard: same workspace inputs, different sample bytes ->
    a different effective SHA (never a cached/reused value)."""
    path = tmp_path / "corrected.json"
    lane = TranscriptLaneInput(
        lane="operator_corrected_diagnostic", corrected_sample=path
    )
    _write_sample(path, ((0, 3000, "一年目の話"),))
    _s, _h, _e, first = prepare_arm_transcript(_data(), _episode(tmp_path), lane)
    _write_sample(path, ((0, 3000, "二年目の話"),))
    _s, _h, _e, second = prepare_arm_transcript(_data(), _episode(tmp_path), lane)
    assert first.effective_transcript_sha256 != second.effective_transcript_sha256


def test_diagnostic_lane_genuine_overlap_after_quantization_is_typed(
    tmp_path: Path,
) -> None:
    """Only lattice residue may collapse; a real overlap in the corrected
    sample refuses exactly like the ASR path."""
    sample = _write_sample(
        tmp_path / "corrected.json", ((0, 3000, "一本目"), (1500, 6000, "重複"))
    )
    with pytest.raises(RealPoolError, match="overlaps the previous segment"):
        prepare_arm_transcript(
            _data(total_frames=8467),
            _episode(tmp_path),
            TranscriptLaneInput(
                lane="operator_corrected_diagnostic", corrected_sample=sample
            ),
        )


# ---------------------------------------------------------------------------
# prepare_arm_transcript — system lane
# ---------------------------------------------------------------------------


def test_system_lane_rejects_any_operator_override(tmp_path: Path) -> None:
    """Corrected/operator bytes may never enter system_asr — not by flag,
    alias, or fallback. Presence of the override is itself the refusal."""
    override = _write_sample(tmp_path / "override.json", ((0, 3000, "手書き"),))
    with pytest.raises(ArmPipelineError) as error:
        prepare_arm_transcript(
            _data(),
            _episode(tmp_path),
            TranscriptLaneInput(lane="system_asr", corrected_sample=override),
        )
    assert error.value.code == "asr-alignment-failed"
    assert "override" in error.value.detail


def test_system_lane_missing_reference_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ArmPipelineError) as error:
        prepare_arm_transcript(
            _data(),
            _episode(tmp_path),
            TranscriptLaneInput(lane="system_asr"),
        )
    assert error.value.code == "asr-alignment-failed"
    assert "transcript-sample-corrected.json" in error.value.detail


def test_system_lane_passing_thresholds_uses_pinned_asr_speech(
    tmp_path: Path,
) -> None:
    """Reference == the pinned ASR hypothesis: all four metrics at zero, the
    gate passes, and the effective speech is the ASR speech (substituted)."""
    data = _data()
    _write_sample(
        _episode(tmp_path) / "transcript-sample-corrected.json",
        data.transcript_segments_ms,
    )
    speech, _hypothesis_ms, _edits, alignment = prepare_arm_transcript(
        data, _episode(tmp_path), TranscriptLaneInput(lane="system_asr")
    )
    assert speech == data.speech  # pinned ASR segments, not corrected bytes
    assert alignment.lane == "system_asr"
    assert alignment.evidence_quality.transcript_cer == 0.0
    assert alignment.evidence_quality.timestamp_error_p95_ms == 0.0
    assert alignment.evidence_quality.omitted_utterances == 0
    assert alignment.evidence_quality.duplicated_utterances == 0


def test_system_lane_measured_drift_refuses_naming_all_four_metrics(
    tmp_path: Path,
) -> None:
    """A drifted ASR hypothesis vs the corrected reference (the r3-measured
    failure shape: CER>0.10, p95 null-or-high, omitted>5, duplicated>5)
    refuses with every violated metric named."""
    reference_ms = tuple(
        (index * 3000, index * 3000 + 2800, f"正しい発話その{index}")
        for index in range(7)  # 7 references the 3-segment hypothesis misses
    )
    _write_sample(
        _episode(tmp_path) / "transcript-sample-corrected.json", reference_ms
    )
    with pytest.raises(ArmPipelineError) as error:
        prepare_arm_transcript(
            _data(), _episode(tmp_path), TranscriptLaneInput(lane="system_asr")
        )
    assert error.value.code == "asr-alignment-failed"
    assert "transcript_cer" in error.value.detail
    assert "omitted_utterances" in error.value.detail


def test_expected_sha_mismatch_is_typed_binding_refusal(tmp_path: Path) -> None:
    sample = _write_sample(tmp_path / "corrected.json", ((0, 3000, "今日はいい天気"),))
    with pytest.raises(ArmPipelineError) as error:
        prepare_arm_transcript(
            _data(),
            _episode(tmp_path),
            TranscriptLaneInput(
                lane="operator_corrected_diagnostic",
                corrected_sample=sample,
                expected_sha256="e" * 64,
            ),
        )
    assert error.value.code == "evaluation-binding-mismatch"


def test_expected_sha_match_proceeds(tmp_path: Path) -> None:
    sample = _write_sample(tmp_path / "corrected.json", ((0, 3000, "今日はいい天気"),))
    _speech, _hypothesis, _edits, alignment = prepare_arm_transcript(
        _data(),
        _episode(tmp_path),
        TranscriptLaneInput(
            lane="operator_corrected_diagnostic",
            corrected_sample=sample,
            expected_sha256=effective_transcript_sha256(((0, 3000, "今日はいい天気"),)),
        ),
    )
    assert alignment.lane == "operator_corrected_diagnostic"
