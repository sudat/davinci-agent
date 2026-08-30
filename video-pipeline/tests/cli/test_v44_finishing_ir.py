from fractions import Fraction
from typing import cast

import pytest

from services.cli._v44_finishing_ir import asr_segments_from_cues
from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.creative_plan.ir_models_v2 import (
    PlacedClipV2,
    SubtitleCueV2,
    TimelineIrV2,
    VideoTrackV2,
)
from services.creative_plan.subtitle_models import (
    PATH_ORDER,
    SubtitleCapabilityPathV1,
    SubtitlePathKind,
    SubtitlePlanCueV1,
    SubtitlePlanV1,
    SubtitleStyleProfileV1,
)
from services.mcp_execution.placement_steps import subtitle_step
from services.mcp_execution.plan_models import MCP_RUNGS
from services.mcp_execution.step_builders import CapabilityView


def test_asr_segments_use_source_timing_when_cue_is_inside_primary_clip() -> None:
    # Given: source frames 100-160 placed at record frames 0-60.
    rate = RationalFrameRate(num=30, den=1)
    ir_v2 = TimelineIrV2(
        schema_version="timeline-ir-v2",
        episode_id="ep-test",
        rate=rate,
        video_tracks=(
            VideoTrackV2(
                role="primary",
                track_id="v-primary",
                items=(
                    PlacedClipV2(
                        item_id="clip-1",
                        source=SourceRef(
                            source_id="source-1",
                            span=SourceFrameSpan(
                                start_frame=100,
                                end_frame=160,
                                rate=rate,
                            ),
                        ),
                        record_span=RecordFrameSpan(start_frame=0, end_frame=60),
                        candidate_ref="candidate-1",
                    ),
                ),
            ),
        ),
        subtitle_cues=(
            SubtitleCueV2(
                cue_id="cue-1",
                text="Test subtitle",
                record_span=RecordFrameSpan(start_frame=15, end_frame=45),
                candidate_ref="candidate-1",
                transcript_ref="transcript-1",
            ),
        ),
    )

    # When: the record-timed cue is converted back to ASR source timing.
    segments, total_source_frames = asr_segments_from_cues(ir_v2, "source-1")

    # Then: the original source range is preserved for the later edit reconciliation.
    assert segments[0].start_seconds == float(Fraction(23, 6))
    assert segments[0].end_seconds == float(Fraction(29, 6))
    assert total_source_frames == 160


# ---------------------------------------------------------------------------
# mcp-complete-parity Task 1 — subtitle seam: payload proof + external gate.
# ---------------------------------------------------------------------------

_SUBTITLE_SEAM_RATE = RationalFrameRate(num=30, den=1)


def _subtitle_seam_plan(selected: SubtitlePathKind) -> SubtitlePlanV1:
    """Two committed cues (record frames 15-45 and 65-86) on the given rung."""

    def cue(
        cue_id: str, text: str, src_start: int, rec_start: int, length: int
    ) -> SubtitlePlanCueV1:
        return SubtitlePlanCueV1(
            cue_id=cue_id,
            transcript_ref=f"{cue_id}-asr",
            source_id="source-1",
            lines=(text,),
            source_span=SourceFrameSpan(
                start_frame=src_start, end_frame=src_start + length, rate=_SUBTITLE_SEAM_RATE
            ),
            record_span=RecordFrameSpan(
                start_frame=rec_start, end_frame=rec_start + length
            ),
        )

    return SubtitlePlanV1(
        schema_version="subtitle-plan-v1",
        episode_id="ep-subtitle-seam",
        rate=_SUBTITLE_SEAM_RATE,
        style_profile=SubtitleStyleProfileV1(profile_id="subtitle-style-default"),
        filler_policy="retain",
        cues=(
            cue("cue-s1", "今日はDaVinci Resolveの使い方を紹介します", 115, 15, 30),
            cue("cue-s2", "再生と編集の違いに注意してください", 165, 65, 21),
            SubtitlePlanCueV1(
                cue_id="cue-s3-long",
                transcript_ref="cue-s3-long-asr",
                source_id="source-1",
                lines=(
                    "これは長い字幕の確認用キューで、表示行として",
                    "二行に分割された日本語テキストを運びます",
                ),
                source_span=SourceFrameSpan(
                    start_frame=200, end_frame=250, rate=_SUBTITLE_SEAM_RATE
                ),
                record_span=RecordFrameSpan(start_frame=100, end_frame=150),
            ),
        ),
        capability_path=SubtitleCapabilityPathV1(
            ordered_paths=PATH_ORDER,
            selected=selected,
            matrix_capability="subtitle-capability",
            matrix_status="accepted" if selected == "native_text_plus" else "failed",
            note="fixture: subtitle seam characterization",
        ),
    )


def test_future_native_subtitle_step_params_carry_exact_committed_cues() -> None:
    """RED until Task 4: the native-path subtitle step must carry every
    committed cue's exact text and record span in its typed payload — a
    cue_count-only payload cannot drive an exact native mutation."""

    plan = _subtitle_seam_plan("native_text_plus")
    step = subtitle_step(plan, CapabilityView({"subtitle-capability": "accepted"}, {}))

    assert step.tool_surface == "subtitle_generation_probe"
    params = step.normalized_params.model_dump(mode="json")
    cues = params.get("cues")
    if not isinstance(cues, list) or len(cues) != len(plan.cues):
        pytest.fail(
            "subtitle step payload carries only cue_count — exact committed cue "
            "text/timing cannot be expressed, so a native exact mutation is "
            "impossible through the current typed payload"
        )
    for committed, actual in zip(plan.cues, cues, strict=True):
        entry = cast("dict[str, object]", actual)
        text = entry.get("text")
        if text is None and isinstance(entry.get("lines"), list):
            text = "\n".join(cast("list[str]", entry["lines"]))
        assert text == "\n".join(committed.lines)
        span = cast("dict[str, object]", entry.get("record_span"))
        assert span.get("start_frame") == committed.record_span.start_frame
        assert span.get("end_frame") == committed.record_span.end_frame


def test_external_remux_subtitle_path_is_never_an_mcp_native_rung() -> None:
    """Gate regression (external remux): a final-v5-style external SRT mux is
    comparison evidence only — the compiled step lands on the external
    executor surface with an explicit fallback record, so it can never count
    as MCP-native subtitle success."""

    step = subtitle_step(_subtitle_seam_plan("external_ass_srt"), CapabilityView({}, {}))

    assert step.tool_surface == "external_asset_builder"
    assert step.rung == "external_asset_render"
    assert step.rung not in MCP_RUNGS
    assert step.fallback_record is not None
