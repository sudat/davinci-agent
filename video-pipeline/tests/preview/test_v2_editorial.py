"""Editorial Preview v2 (task 60): Timeline IR v2 -> MP4 + PreviewTraceV2.

Covers PRD 13.1 Editorial Preview content at documented rough fidelity:
story-order primary concat, full-frame B-roll cut-ins, SRT sidecar + soft
mov_text近似字幕, placeholder title cards, placeholder tone music, and
Moment Deep Review flags (manifest + burned red corner marker).

Frame-content checks decode single frames to raw RGB and assert solid-color
dominance with codec-loss tolerance (the synthetic B-roll/card/marker sources
are solid colors, so mid-span frames stay within a few units of the pure
value after h264; boundary frames are deliberately avoided because overlay
``enable`` windows carry a +-1 frame boundary tolerance).

Determinism: same inputs -> identical DECODED video sha and sidecar bytes
(container bytes may differ under h264_videotoolbox; v1 precedent).
"""

from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.creative_plan.ir_models_v2 import (
    AudioItemV2,
    AudioTrackV2,
    EffectIntentV2,
    PlacedClipV2,
    SubtitleCueV2,
    TimelineIrV2,
    VideoTrackV2,
)
from services.creative_plan.subtitle_models import (
    DEFAULT_STYLE_PROFILE,
    PATH_ORDER,
    SubtitleCapabilityPathV1,
    SubtitlePlanCueV1,
    SubtitlePlanV1,
)
from services.foundation_io import sha256_file
from services.preview import v2_editorial
from services.preview.errors import PreviewBindingError, PreviewLayoutError
from services.preview.srt import parse_srt
from services.preview.tools import demux_subtitle, run_bounded
from services.preview.v2_editorial import render_editorial_preview
from services.preview.v2_models import (
    PreviewTraceV2,
    SourceMediaEntryV2,
    SourceMediaMapV2,
)

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools

RATE = RationalFrameRate(num=30, den=1)
EPISODE = "ep-v2-preview"
PREVIEW_MP4 = "editorial-preview.mp4"


def _clip(
    item_id: str, source_id: str, src_start: int, src_end: int, rec_start: int
) -> PlacedClipV2:
    return PlacedClipV2(
        item_id=item_id,
        source=SourceRef(
            source_id=source_id,
            span=SourceFrameSpan(start_frame=src_start, end_frame=src_end, rate=RATE),
        ),
        record_span=RecordFrameSpan(
            start_frame=rec_start, end_frame=rec_start + (src_end - src_start)
        ),
        candidate_ref=f"cand-{item_id.removeprefix('itm-')}",
    )


def build_v2_ir() -> TimelineIrV2:
    """180-frame fixture: primary story cut, green B-roll cut-in, title card,
    dialogue+placeholder-music audio, two JA cues, one manual_required flag."""

    return TimelineIrV2(
        schema_version="timeline-ir-v2",
        episode_id=EPISODE,
        rate=RATE,
        video_tracks=(
            VideoTrackV2(
                role="primary",
                track_id="v-primary",
                items=(
                    _clip("itm-cand-a", "cam-a", 0, 90, 0),
                    _clip("itm-cand-b", "cam-a", 90, 180, 90),
                ),
            ),
            VideoTrackV2(
                role="b_roll",
                track_id="v-broll",
                items=(_clip("itm-broll-1", "cam-b", 0, 30, 30),),
            ),
            VideoTrackV2(
                role="graphic",
                track_id="v-graphic",
                items=(_clip("itm-title-1", "graphic-title-1", 0, 30, 90),),
            ),
        ),
        subtitle_cues=(
            SubtitleCueV2(
                cue_id="cue-1",
                text="こんにちは",
                record_span=RecordFrameSpan(start_frame=9, end_frame=60),
                candidate_ref="cand-cand-a",
                transcript_ref="seg-1",
            ),
            SubtitleCueV2(
                cue_id="cue-2",
                text="説明します",
                record_span=RecordFrameSpan(start_frame=99, end_frame=150),
                candidate_ref="cand-cand-b",
                transcript_ref="seg-2",
            ),
        ),
        audio_tracks=(
            AudioTrackV2(
                role="dialogue",
                track_id="a-dialogue",
                items=(
                    AudioItemV2(
                        item_id="aud-cand-a",
                        source=SourceRef(
                            source_id="cam-a",
                            span=SourceFrameSpan(start_frame=0, end_frame=90, rate=RATE),
                        ),
                        record_span=RecordFrameSpan(start_frame=0, end_frame=90),
                        candidate_ref="cand-cand-a",
                    ),
                    AudioItemV2(
                        item_id="aud-cand-b",
                        source=SourceRef(
                            source_id="cam-a",
                            span=SourceFrameSpan(start_frame=90, end_frame=180, rate=RATE),
                        ),
                        record_span=RecordFrameSpan(start_frame=90, end_frame=180),
                        candidate_ref="cand-cand-b",
                    ),
                ),
            ),
            AudioTrackV2(
                role="music",
                track_id="a-music",
                items=(
                    AudioItemV2(
                        item_id="itm-music-1",
                        source=SourceRef(
                            source_id="music-bed-1",
                            span=SourceFrameSpan(start_frame=0, end_frame=180, rate=RATE),
                        ),
                        record_span=RecordFrameSpan(start_frame=0, end_frame=180),
                    ),
                ),
            ),
        ),
        effect_intents=(
            EffectIntentV2(
                effect_id="fx-review-1",
                kind="manual_required",
                target_item_id="itm-cand-b",
                note="音声不明瞭",
            ),
        ),
    )


def build_subtitle_plan(ir: TimelineIrV2) -> SubtitlePlanV1:
    return SubtitlePlanV1(
        schema_version="subtitle-plan-v1",
        episode_id=ir.episode_id,
        rate=ir.rate,
        style_profile=DEFAULT_STYLE_PROFILE,
        filler_policy="retain",
        cues=(
            SubtitlePlanCueV1(
                cue_id="cue-1",
                transcript_ref="seg-1",
                source_id="cam-a",
                lines=("こんにちは",),
                source_span=SourceFrameSpan(start_frame=0, end_frame=51, rate=RATE),
                record_span=RecordFrameSpan(start_frame=9, end_frame=60),
            ),
            SubtitlePlanCueV1(
                cue_id="cue-2",
                transcript_ref="seg-2",
                source_id="cam-a",
                lines=("説明します",),
                source_span=SourceFrameSpan(start_frame=0, end_frame=51, rate=RATE),
                record_span=RecordFrameSpan(start_frame=99, end_frame=150),
            ),
        ),
        capability_path=SubtitleCapabilityPathV1(
            ordered_paths=PATH_ORDER,
            selected="native_text_plus",
            matrix_capability="subtitle-capability",
            matrix_status="accepted",
            note="task-60-fixture",
        ),
    )


def _generate_media(
    tools: PinnedTools, path: Path, video_graph: str, seconds: int, freq: int
) -> None:
    run_bounded(
        [
            str(tools.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            video_graph,
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:sample_rate=48000",
            "-t",
            str(seconds),
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            "-video_track_timescale",
            "30000",
            "-r",
            "30",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(path),
        ]
    )


@pytest.fixture(scope="session")
def v2_media(tools: PinnedTools, tmp_path_factory: pytest.TempPathFactory) -> SourceMediaMapV2:
    root = tmp_path_factory.mktemp("v2-media")
    cam_a = root / "cam-a.mov"
    cam_b = root / "cam-b.mov"
    _generate_media(tools, cam_a, "testsrc2=size=320x180:rate=30", 6, 600)
    _generate_media(tools, cam_b, "color=c=0x00FF00:size=320x180:rate=30", 2, 440)
    return SourceMediaMapV2(
        entries=(
            SourceMediaEntryV2(source_id="cam-a", media_path=str(cam_a), sha256=sha256_file(cam_a)),
            SourceMediaEntryV2(source_id="cam-b", media_path=str(cam_b), sha256=sha256_file(cam_b)),
        )
    )


@pytest.fixture(scope="session")
def v2_render(
    tools: PinnedTools, v2_media: SourceMediaMapV2, tmp_path_factory: pytest.TempPathFactory
) -> tuple[PreviewTraceV2, Path]:
    out_dir = tmp_path_factory.mktemp("v2-render")
    return (
        render_editorial_preview(
            build_v2_ir(),
            subtitle_plan=build_subtitle_plan(build_v2_ir()),
            source_media_map=v2_media,
            output_path=out_dir / PREVIEW_MP4,
            tools=tools,
        ),
        out_dir / PREVIEW_MP4,
    )


def _frame_mean_rgb(
    tools: PinnedTools, mp4: Path, seconds: str, crop: str | None = None
) -> tuple[float, float, float]:
    """Decode one frame to raw RGB and return per-channel means (binary-safe)."""

    argv = [
        str(tools.ffmpeg),
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(mp4),
        "-ss",
        seconds,
        "-frames:v",
        "1",
    ]
    if crop is not None:
        argv += ["-vf", crop]
    argv += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    result = subprocess.run(argv, check=False, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()[-500:]
    raw = result.stdout
    assert len(raw) >= 3 * 24 * 24
    pixels = len(raw) // 3
    return (
        sum(raw[0::3]) / pixels,
        sum(raw[1::3]) / pixels,
        sum(raw[2::3]) / pixels,
    )


def test_editorial_preview_renders_expected_extent_and_streams(
    v2_render: tuple[PreviewTraceV2, Path],
) -> None:
    """(a) IR v2 fixture -> MP4 with the exact record extent + trace sha."""

    trace, preview = v2_render
    assert preview.is_file()
    assert preview.stat().st_size > 0
    assert trace.preview.path == str(preview)
    assert trace.preview.sha256 == sha256_file(preview)
    assert trace.preview.size == preview.stat().st_size
    assert len(trace.preview.decoded_video_sha256) == 64
    assert trace.total_record_frames == 180
    summary = trace.ffprobe_summary
    assert summary.nb_read_frames == 180
    assert summary.video_duration_ms == 6000
    assert (summary.width, summary.height) == (640, 360)
    assert summary.video_codec == "h264"
    assert summary.r_frame_rate == summary.avg_frame_rate == "30/1"
    assert (summary.audio_codec, summary.audio_sample_rate, summary.audio_channels) == (
        "aac",
        48000,
        1,
    )
    assert summary.subtitle_codec == "mov_text"
    assert trace.rate.as_fraction == Fraction(30, 1)


def test_b_roll_cut_in_and_title_card_pixels(
    tools: PinnedTools, v2_render: tuple[PreviewTraceV2, Path]
) -> None:
    """(b) B-roll cut-in present: mid-span frame is the synthetic green source
    (solid-color dominance with codec tolerance); outside the span it is not.
    The placeholder title card is a white card at its span."""

    _, preview = v2_render
    # record frame 45 (1.5s) is mid B-roll span [30,60): full-frame green.
    red, green, blue = _frame_mean_rgb(tools, preview, "1.5")
    assert green > 200
    assert red < 60
    assert blue < 60
    # record frame 135 (4.5s) is primary testsrc2: not dominated by green.
    red, green, blue = _frame_mean_rgb(tools, preview, "4.5")
    assert red > 60
    # record frame 105 (3.5s) is mid title span [90,120): white card center.
    red, green, blue = _frame_mean_rgb(tools, preview, "3.5", crop="crop=320:180:160:90")
    assert red > 200
    assert green > 200
    assert blue > 200


def test_subtitle_sidecar_matches_cues_and_soft_track_round_trips(
    tools: PinnedTools, v2_render: tuple[PreviewTraceV2, Path]
) -> None:
    """(c) Sidecar SRT equals the subtitle-plan cues and the muxed soft
    mov_text track demuxes back to the same cues."""

    trace, preview = v2_render
    sidecar_record = trace.subtitle_sidecar
    assert sidecar_record is not None
    sidecar = Path(sidecar_record.path)
    assert sidecar == preview.with_suffix(".srt")
    assert sidecar_record.sha256 == sha256_file(sidecar)
    assert sidecar_record.cue_count == 2
    cues = parse_srt(sidecar.read_bytes())
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in cues] == [
        (300, 2000, "こんにちは"),
        (3300, 5000, "説明します"),
    ]
    demuxed = parse_srt(demux_subtitle(tools, preview))
    assert demuxed == cues


def test_deep_review_flags_listed_and_marker_burned(
    tools: PinnedTools, v2_render: tuple[PreviewTraceV2, Path]
) -> None:
    """(d) manual_required intents surface as trace flags and the burned red
    corner marker is present at the flagged span only."""

    trace, preview = v2_render
    assert [(flag.at_frame, flag.review_ref, flag.reason) for flag in trace.flags] == [
        (90, "fx-review-1", "音声不明瞭")
    ]
    # record frame 105 (3.5s) is inside the flag marker window: red corner.
    red, green, blue = _frame_mean_rgb(tools, preview, "3.5", crop="crop=24:24:608:8")
    assert red > 200
    assert green < 60
    assert blue < 60
    # record frame 30 (1.0s) has no marker; the corner shows the B-roll green.
    red, green, blue = _frame_mean_rgb(tools, preview, "1.0", crop="crop=24:24:608:8")
    assert green > 200
    assert red < 60


def test_unknown_track_role_refused(
    tools: PinnedTools, v2_media: SourceMediaMapV2, tmp_path: Path
) -> None:
    """(e) Unknown track role -> explicit typed error, never a silent skip.

    The IR Literal rejects unknown roles at the front door (asserted via a
    literal_error); the renderer still guards its own supported-role sets
    because the Literal may grow faster than the renderer (bypassed via
    model_construct, which skips validation).
    """

    payload = build_v2_ir().model_dump(mode="json")
    payload["video_tracks"] = [
        {
            "role": "mystery",
            "track_id": "v-unknown",
            "items": [payload["video_tracks"][0]["items"][0]],
        }
    ]
    with pytest.raises(ValidationError) as excinfo:
        TimelineIrV2.model_validate(payload, strict=False)
    assert any(error["type"] == "literal_error" for error in excinfo.value.errors())

    ghost = VideoTrackV2.model_construct(
        role="mystery",
        track_id="v-unknown",
        items=(build_v2_ir().video_tracks[0].items[0],),
    )
    ir = build_v2_ir().model_construct(video_tracks=(ghost,), audio_tracks=())
    with pytest.raises(PreviewLayoutError, match="mystery"):
        v2_editorial.extract_editorial_layout(ir, v2_media, tools=tools)


def test_determinism_same_inputs_identical_semantics(
    tools: PinnedTools,
    v2_media: SourceMediaMapV2,
    v2_render: tuple[PreviewTraceV2, Path],
    tmp_path: Path,
) -> None:
    """(f) Same inputs -> identical decoded video sha + sidecar bytes +
    ffprobe summary (container bytes may differ; v1 semantic-equivalence)."""

    trace_first, _ = v2_render
    trace_second = render_editorial_preview(
        build_v2_ir(),
        subtitle_plan=build_subtitle_plan(build_v2_ir()),
        source_media_map=v2_media,
        output_path=tmp_path / PREVIEW_MP4,
        tools=tools,
    )
    assert trace_second.preview.decoded_video_sha256 == trace_first.preview.decoded_video_sha256
    assert trace_second.ffprobe_summary == trace_first.ffprobe_summary
    assert trace_second.flags == trace_first.flags
    assert trace_second.cut_ins == trace_first.cut_ins
    first_sidecar = trace_first.subtitle_sidecar
    second_sidecar = trace_second.subtitle_sidecar
    assert first_sidecar is not None
    assert second_sidecar is not None
    assert Path(second_sidecar.path).read_bytes() == Path(first_sidecar.path).read_bytes()


def test_subtitle_plan_none_falls_back_to_ir_cues(
    tools: PinnedTools, v2_media: SourceMediaMapV2, tmp_path: Path
) -> None:
    """No subtitle plan -> sidecar built from the IR v2 subtitle cues."""

    trace = render_editorial_preview(
        build_v2_ir(),
        subtitle_plan=None,
        source_media_map=v2_media,
        output_path=tmp_path / PREVIEW_MP4,
        tools=tools,
    )
    sidecar_record = trace.subtitle_sidecar
    assert sidecar_record is not None
    cues = parse_srt(Path(sidecar_record.path).read_bytes())
    assert [cue.text for cue in cues] == ["こんにちは", "説明します"]


def test_subtitle_plan_disagreement_refused(
    tools: PinnedTools, v2_media: SourceMediaMapV2, tmp_path: Path
) -> None:
    """A subtitle plan from another episode is a typed refusal."""

    plan = build_subtitle_plan(build_v2_ir()).model_copy(update={"episode_id": "ep-other"})
    with pytest.raises(PreviewBindingError, match="episode"):
        render_editorial_preview(
            build_v2_ir(),
            subtitle_plan=plan,
            source_media_map=v2_media,
            output_path=tmp_path / PREVIEW_MP4,
            tools=tools,
        )


def test_missing_media_binding_refused(
    tools: PinnedTools, v2_media: SourceMediaMapV2, tmp_path: Path
) -> None:
    """A placed clip whose source lacks a binding is a typed refusal."""

    partial = SourceMediaMapV2(entries=(v2_media.entries[0],))
    with pytest.raises(PreviewBindingError, match="cam-b"):
        render_editorial_preview(
            build_v2_ir(),
            subtitle_plan=None,
            source_media_map=partial,
            output_path=tmp_path / PREVIEW_MP4,
            tools=tools,
        )


def test_flag_targeting_unknown_item_refused(
    tools: PinnedTools, v2_media: SourceMediaMapV2
) -> None:
    """A manual_required flag pointing at a ghost item is a typed refusal."""

    ir = build_v2_ir().model_copy(
        update={
            "effect_intents": (
                build_v2_ir().effect_intents[0].model_copy(update={"target_item_id": "itm-ghost"}),
            )
        }
    )
    with pytest.raises(PreviewLayoutError, match="itm-ghost"):
        v2_editorial.extract_editorial_layout(ir, v2_media, tools=tools)
