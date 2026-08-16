"""Happy-path rendering: frozen 0A binding, subtitle anchor, repeat equivalence."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from services.contracts.primitives import RationalFrameRate, RecordFrameSpan
from services.preview.binding import initial_bindings, initial_timeline_ir
from services.preview.render import render_preview
from services.preview.srt import cue_from_record_span, parse_srt
from services.preview.tools import demux_subtitle

if TYPE_CHECKING:
    from services.fixtures.manifest import Phase0AFixtureManifest
    from services.preview.models import PreviewTraceManifest
    from services.preview.tools import PinnedTools


def test_initial_preview_trace_covers_frozen_record_layout(
    initial_render: tuple[PreviewTraceManifest, Path],
) -> None:
    trace, preview = initial_render
    assert trace.timeline_binding.plan_version == "v1"
    assert trace.timeline_binding.total_record_frames == 660
    assert trace.timeline_binding.timeline_rate.as_fraction == Fraction(30, 1)
    spans = [(entry.span.start_frame, entry.span.end_frame) for entry in trace.record_to_decision]
    assert spans == [(0, 30), (30, 330), (330, 630), (630, 660)]
    assert {entry.decision_id for entry in trace.record_to_decision} == {"initial-plan-v1"}
    assert preview.is_file()
    assert Path(trace.preview.path) == preview
    assert len(trace.inputs) == 9
    video_inputs = [entry for entry in trace.inputs if entry.kind == "video"]
    assert [entry.item_id for entry in video_inputs] == [
        "intro-001",
        "cut-001",
        "cut-002",
        "outro-001",
    ]
    assert all(entry.media_path.endswith(".mov") for entry in video_inputs)
    assert all(entry.sha256 for entry in trace.inputs)


def test_initial_preview_stream_layout_and_strategy_notes(
    initial_render: tuple[PreviewTraceManifest, Path],
) -> None:
    trace, _ = initial_render
    summary = trace.ffprobe_summary
    assert (summary.stream_count, summary.video_codec) == (3, "h264")
    assert (summary.width, summary.height) == (640, 360)
    assert summary.r_frame_rate == summary.avg_frame_rate == "30/1"
    assert summary.nb_read_frames == 660
    assert summary.video_duration_ms == 22000
    assert (summary.audio_codec, summary.audio_sample_rate, summary.audio_channels) == (
        "aac",
        48000,
        1,
    )
    assert summary.subtitle_codec == "mov_text"
    assert trace.strategy_notes.determinism_policy == "semantic-equivalence-h264-videotoolbox"
    assert "mov-text" in trace.strategy_notes.subtitle_rung
    assert "overlay" in trace.strategy_notes.overlay_strategy
    assert "amix" in trace.strategy_notes.audio_strategy


def test_subtitle_anchor_round_trips_the_frozen_table_shifted_to_record_coords(
    tools: PinnedTools,
    fixture_dir: Path,
    manifest: Phase0AFixtureManifest,
    initial_render: tuple[PreviewTraceManifest, Path],
) -> None:
    trace, preview = initial_render
    table = json.loads((fixture_dir / "subtitle-table.json").read_bytes())
    recipe = manifest.recipe.subtitle
    assert table["record_span"]["start_frame"] == recipe.record_span.start_frame
    assert table["record_span"]["end_frame"] == recipe.record_span.end_frame
    assert table["text"] == recipe.text
    cues = parse_srt(demux_subtitle(tools, preview))
    expected = (
        cue_from_record_span(
            RecordFrameSpan(
                start_frame=recipe.record_span.start_frame,
                end_frame=recipe.record_span.end_frame,
            ),
            RationalFrameRate(num=30, den=1),
            recipe.text,
        ),
    )
    assert cues == expected
    assert trace.ffprobe_summary.subtitle_codec == "mov_text"


def test_repeat_render_is_semantically_equivalent(
    tools: PinnedTools,
    fixture_dir: Path,
    manifest: Phase0AFixtureManifest,
    initial_render: tuple[PreviewTraceManifest, Path],
    tmp_path: Path,
) -> None:
    trace_first, _ = initial_render
    ir = initial_timeline_ir(manifest)
    bindings = initial_bindings(fixture_dir)
    trace_second = render_preview(None, ir, bindings, tmp_path, tools=tools)
    # Container bytes may differ (h264_videotoolbox); decoded frames must not.
    assert trace_second.preview.decoded_video_sha256 == trace_first.preview.decoded_video_sha256
    assert trace_second.ffprobe_summary == trace_first.ffprobe_summary
    assert trace_second.record_to_decision == trace_first.record_to_decision
    assert trace_second.timeline_binding == trace_first.timeline_binding
    assert trace_second.inputs == trace_first.inputs
