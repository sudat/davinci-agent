"""Preview trace checks: binding, rate, coverage, duration, subtitle presence."""

from __future__ import annotations

from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
)
from services.preview.models import (
    FfprobeSummary,
    PreviewFile,
    PreviewTraceManifest,
    RecordDecisionSpan,
    StrategyNotes,
    TimelineBinding,
    TraceDecision,
    TraceInput,
)
from services.qc.checks import check_preview
from tests.qc.support import clean_policy, preset, rehash

INPUTS = ("e" * 64,)


def _trace(
    *,
    rate: str = "30/1",
    frames: int = 120,
    duration_ms: int = 4000,
    subtitle: bool = False,
) -> PreviewTraceManifest:
    rate_model = RationalFrameRate(num=30, den=1)
    inputs = [
        TraceInput(
            item_id="v-0",
            kind="video",
            media_path="/media/source.mov",
            sha256="0" * 64,
            source_span=SourceFrameSpan(start_frame=0, end_frame=120, rate=rate_model),
            record_span=RecordFrameSpan(start_frame=0, end_frame=120),
        ),
        TraceInput(
            item_id="a-0",
            kind="audio",
            media_path="/media/source.mov",
            sha256="0" * 64,
            source_span=SourceFrameSpan(start_frame=0, end_frame=120, rate=rate_model),
            record_span=RecordFrameSpan(start_frame=0, end_frame=120),
        ),
    ]
    if subtitle:
        inputs.append(
            TraceInput(
                item_id="cue-0",
                kind="subtitle",
                media_path="/media/sub.srt",
                sha256="0" * 64,
                source_span=SourceFrameSpan(start_frame=0, end_frame=60, rate=rate_model),
                record_span=RecordFrameSpan(start_frame=0, end_frame=60),
            )
        )
    return PreviewTraceManifest(
        schema_version="preview-trace-v1",
        preview=PreviewFile(
            path="previews/preview.mp4",
            sha256="0" * 64,
            size=1234,
            decoded_video_sha256="0" * 64,
        ),
        timeline_binding=TimelineBinding(
            plan_version="v1",
            ir_sha256="0" * 64,
            total_record_frames=frames,
            timeline_rate=rate_model,
        ),
        inputs=tuple(inputs),
        decisions=(
            TraceDecision(
                decision_id="d-1",
                case_id="c-1",
                classification="initial",
                applied=False,
                plan_version_after="v1",
            ),
        ),
        record_to_decision=(
            RecordDecisionSpan(
                span=RecordFrameSpan(start_frame=0, end_frame=frames),
                decision_id="d-1",
            ),
        ),
        strategy_notes=StrategyNotes(
            subtitle_rung="generated-srt",
            overlay_strategy="none",
            audio_strategy="copy",
            determinism_policy="semantic-equivalence-h264-videotoolbox",
        ),
        ffprobe_summary=FfprobeSummary(
            stream_count=2,
            video_codec="h264",
            width=640,
            height=360,
            r_frame_rate=rate,
            avg_frame_rate=rate,
            nb_read_frames=frames,
            video_duration_ms=duration_ms,
            container_duration_ms=duration_ms + 100,
            audio_codec="aac",
            audio_sample_rate=48000,
            audio_channels=1,
            subtitle_codec="mov_text" if subtitle else None,
        ),
    )


def rules(issues) -> set[str]:
    return {issue.rule_id for issue in issues}


def test_clean_trace_passes() -> None:
    assert check_preview(_trace(), clean_policy(preset()), INPUTS) == ()


def test_missing_binding_blocks_only_when_required() -> None:
    policy = clean_policy(preset())
    assert check_preview(None, policy, INPUTS) == ()
    required = rehash(
        policy.model_copy(
            update={"preview": policy.preview.model_copy(update={"require_binding": True})}
        )
    )
    assert rules(check_preview(None, required, INPUTS)) == {"preview_binding_missing"}


def test_rate_drift_blocks() -> None:
    trace = _trace(rate="25/1")
    assert "preview_rate_drift" in rules(check_preview(trace, clean_policy(preset()), INPUTS))


def test_frame_coverage_drift_blocks() -> None:
    trace = _trace(frames=120, duration_ms=4000)
    trace = trace.model_copy(
        update={
            "timeline_binding": trace.timeline_binding.model_copy(
                update={"total_record_frames": 100}
            )
        }
    )
    assert "preview_trace_coverage" in rules(
        check_preview(trace, clean_policy(preset()), INPUTS)
    )


def test_duration_drift_blocks() -> None:
    trace = _trace(duration_ms=4300)
    assert "preview_duration_drift" in rules(
        check_preview(trace, clean_policy(preset()), INPUTS)
    )


def test_subtitle_presence_mismatch_blocks() -> None:
    policy = clean_policy(preset())
    expect_sub = rehash(
        policy.model_copy(
            update={"preview": policy.preview.model_copy(update={"subtitle_expected": True})}
        )
    )
    assert "preview_subtitle_presence" in rules(check_preview(_trace(), expect_sub, INPUTS))
