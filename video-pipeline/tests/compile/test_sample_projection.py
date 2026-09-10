"""Wave-1 sample projection regressions (v4 contract 訂正1/訂正2 + F5 + P1-6).

Pure-function tests over a purpose-built minimal IR (video+audio+subtitle
with known record spans): deterministic ids within the Identifier
charset, Record-window-only coordinates, clip-only source shifts,
gapless video/audio re-lay, subtitle gap/overlap rules, zero-cue track
omission, FULL window-digest artifact ids (P1-6: untruncated, so the
same normalized windows always yield the same id and different windows
always yield a different one), the exactly-one-video-plus-one-audio
track-count gate plus AV pairing over shared record intervals
(P1-6: typed failure on mis-pair), the source/record span
precondition, the zero-length window refusal, and typed failures raised
before any rendering.
"""

from __future__ import annotations

import re
from typing import Literal

import pytest
from pydantic_core import PydanticCustomError

from services.compile.sample_projection import (
    derive_sample_windows,
    project_sample_ir,
    sample_total_frames,
    sample_total_seconds,
    window_digest,
    windows_around_anchors,
)
from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.timeline_ir import (
    TimelineIr0C,
    TimelineItem0C,
    TimelineTrack0C,
    TrackRef0C,
)

RATE = RationalFrameRate(num=30, den=1)
PRODUCER = Producer(name="sample-projection-tests", version="1")


def _item(  # noqa: PLR0913 (fixture builder: one field per item slot)
    item_id: str,
    kind: Literal["video", "audio", "subtitle"],
    track_index: int,
    record: tuple[int, int],
    *,
    av_link: str | None = None,
    text: str | None = None,
) -> TimelineItem0C:
    return TimelineItem0C(
        item_id=item_id,
        kind=kind,
        source=SourceRef(
            source_id=f"src-{track_index}",
            span=SourceFrameSpan(
                start_frame=record[0], end_frame=record[1], rate=RATE
            ),
        ),
        record_span=RecordFrameSpan(start_frame=record[0], end_frame=record[1]),
        av_link_id=av_link,
        subtitle_text=text,
    )


def _full_ir() -> TimelineIr0C:
    """video/audio A1[0,60) A2[60,120) linked pairwise; cue S1[10,50)."""
    video = TimelineTrack0C(
        track=TrackRef0C(kind="video", index=1),
        items=(
            _item("v1", "video", 1, (0, 60), av_link="av1"),
            _item("v2", "video", 1, (60, 120), av_link="av2"),
        ),
    )
    audio = TimelineTrack0C(
        track=TrackRef0C(kind="audio", index=2),
        items=(
            _item("a1", "audio", 2, (0, 60), av_link="av1"),
            _item("a2", "audio", 2, (60, 120), av_link="av2"),
        ),
    )
    subtitle = TimelineTrack0C(
        track=TrackRef0C(kind="subtitle", index=3),
        items=(_item("s1", "subtitle", 3, (10, 50), text="最初の一文です"),),
    )
    return TimelineIr0C(
        artifact_id="ir-full-test",
        artifact_type="timeline_ir_0c",
        schema_version="timeline-ir-0c-v1",
        content_hash="f" * 64,
        producer=PRODUCER,
        inputs=(),
        rate=RATE,
        tracks=(video, audio, subtitle),
    )


def _span(start: int, end: int) -> RecordFrameSpan:
    return RecordFrameSpan(start_frame=start, end_frame=end)


def test_deterministic_ids_order_independence_and_shape() -> None:
    ir = _full_ir()
    sample = project_sample_ir(ir, [_span(0, 30), _span(60, 90)])
    reordered = project_sample_ir(ir, [_span(60, 90), _span(0, 30)])
    assert sample == reordered
    assert sample_total_frames([_span(0, 30), _span(60, 90)]) == 60
    assert sample_total_seconds([_span(0, 30), _span(60, 90)], RATE) == pytest.approx(2.0)
    video = next(t for t in sample.tracks if t.track.kind == "video")
    assert [i.item_id for i in video.items] == ["v1.s0", "v2.s1"]
    assert video.items[0].record_span == _span(0, 30)
    assert video.items[1].record_span == _span(30, 60)
    assert video.items[0].av_link_id == "av1"
    expected = f"ir-full-test-sample-{window_digest([_span(0, 30), _span(60, 90)])}"
    assert sample.artifact_id == expected


def test_window_digest_suffix_is_full_hex() -> None:
    """P1-6: the id carries the FULL digest — no truncation."""
    digest = window_digest([_span(0, 30)])
    assert re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    sample = project_sample_ir(_full_ir(), [_span(0, 30)])
    head = sample.artifact_id.rsplit("-", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{64}", head) is not None
    assert sample.artifact_id == f"ir-full-test-sample-{digest}"


def test_different_windows_always_different_id() -> None:
    """P1-6: any window change yields a different id (truncation hid this)."""
    base = _full_ir()
    ids = {
        project_sample_ir(base, w).artifact_id
        for w in (
            [_span(0, 30)],
            [_span(0, 31)],
            [_span(1, 30)],
            [_span(0, 30), _span(60, 90)],
            [_span(0, 30), _span(60, 91)],
        )
    }
    assert len(ids) == 5
    assert project_sample_ir(base, [_span(60, 90), _span(0, 30)]).artifact_id == (
        project_sample_ir(base, [_span(0, 30), _span(60, 90)]).artifact_id
    )


def test_reordered_windows_share_identity_and_differ_from_changed() -> None:
    base = _full_ir()
    a = project_sample_ir(base, [_span(0, 30), _span(60, 90)])
    assert a.artifact_id == project_sample_ir(
        base, [_span(60, 90), _span(0, 30)]
    ).artifact_id
    assert a.content_hash == project_sample_ir(
        base, [_span(60, 90), _span(0, 30)]
    ).content_hash
    shifted = project_sample_ir(base, [_span(0, 30), _span(60, 91)])
    assert shifted.artifact_id != a.artifact_id
    assert shifted.content_hash != a.content_hash


def test_source_shift_is_clip_only_no_scaling() -> None:
    sample = project_sample_ir(_full_ir(), [_span(10, 40)])
    video = next(t for t in sample.tracks if t.track.kind == "video")
    item = video.items[0]
    assert item.item_id == "v1.s0"
    assert item.source.span.start_frame == 10
    assert item.source.span.end_frame == 40
    assert (
        item.source.span.end_frame - item.source.span.start_frame
        == item.record_span.end_frame - item.record_span.start_frame
    )


def test_item_split_across_windows_gets_one_id_each() -> None:
    sample = project_sample_ir(_full_ir(), [_span(0, 20), _span(20, 40)])
    video = next(t for t in sample.tracks if t.track.kind == "video")
    assert [i.item_id for i in video.items] == ["v1.s0", "v1.s1"]
    assert video.items[0].record_span == _span(0, 20)
    assert video.items[1].record_span == _span(20, 40)


def test_zero_cue_window_omits_subtitle_track() -> None:
    sample = project_sample_ir(_full_ir(), [_span(70, 100)])
    assert [t.track.kind for t in sample.tracks] == ["video", "audio"]
    with_cue = project_sample_ir(_full_ir(), [_span(10, 40)])
    assert any(t.track.kind == "subtitle" for t in with_cue.tracks)
    cue = next(i for t in with_cue.tracks if t.track.kind == "subtitle" for i in t.items)
    assert cue.item_id == "s1.s0"
    assert cue.record_span == _span(0, 30)


def test_window_overlap_is_typed_failure() -> None:
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(_full_ir(), [_span(0, 30), _span(20, 40)])
    assert "sample-window-overlap" in repr(exc_info.value)


def test_empty_windows_is_typed_failure() -> None:
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(_full_ir(), [])
    assert "sample-windows-empty" in repr(exc_info.value)


def test_zero_length_window_is_typed_failure() -> None:
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(_full_ir(), [_span(10, 10)])
    assert "sample-window-empty-span" in repr(exc_info.value)
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(_full_ir(), [_span(0, 30), _span(40, 40)])
    assert "sample-window-empty-span" in repr(exc_info.value)


def test_source_record_span_mismatch_is_typed_failure() -> None:
    ir = _full_ir()
    video = ir.tracks[0]
    scaled = video.items[0].model_copy(
        update={"record_span": RecordFrameSpan(start_frame=0, end_frame=30)}
    )
    remapped = ir.model_copy(
        update={"tracks": (video.model_copy(update={"items": (scaled, video.items[1])}),)}
    )
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(remapped, [_span(0, 30)])
    assert "sample-item-span-mismatch" in repr(exc_info.value)


def test_window_beyond_timeline_is_typed_empty_track() -> None:
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(_full_ir(), [_span(200, 230)])
    assert "sample-empty-track" in repr(exc_info.value)


def test_content_hash_binds_full_ir_and_windows() -> None:
    base = _full_ir()
    a = project_sample_ir(base, [_span(0, 30)])
    b = project_sample_ir(base, [_span(0, 31)])
    assert a.content_hash != b.content_hash
    assert project_sample_ir(base, [_span(0, 30)]).content_hash == a.content_hash


def _mislinked_ir(audio_link: str | None) -> TimelineIr0C:
    """Same spans as the fixture, but a1's link no longer pairs with v1."""
    ir = _full_ir()
    audio = ir.tracks[1]
    relinked = audio.items[0].model_copy(update={"av_link_id": audio_link})
    tracks = (
        ir.tracks[0],
        audio.model_copy(update={"items": (relinked, audio.items[1])}),
        ir.tracks[2],
    )
    return ir.model_copy(update={"tracks": tracks})


def test_av_pairing_mismatch_is_typed_failure() -> None:
    """P1-6: video dubbed over foreign audio never silently mis-pairs."""
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(_mislinked_ir("av2"), [_span(0, 30)])
    assert "sample-av-pairing-mismatch" in repr(exc_info.value)


def test_av_pairing_missing_link_is_typed_failure() -> None:
    """P1-6: a null link on either side of a shared interval fails closed."""
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(_mislinked_ir(None), [_span(0, 30)])
    assert "sample-av-pairing-mismatch" in repr(exc_info.value)


def test_av_track_count_missing_audio_is_typed_failure() -> None:
    """The sample renderer needs exactly one audio track — a video-only
    IR stops typed before any render (never a silent skip)."""
    ir = _full_ir()
    video_only = ir.model_copy(update={"tracks": (ir.tracks[0], ir.tracks[2])})
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(video_only, [_span(0, 30)])
    assert "sample-av-track-count" in repr(exc_info.value)


def test_av_track_count_missing_video_is_typed_failure() -> None:
    """The sample renderer needs exactly one video track — an audio-only
    IR stops typed before any render."""
    ir = _full_ir()
    audio_only = ir.model_copy(update={"tracks": (ir.tracks[1],)})
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(audio_only, [_span(0, 30)])
    assert "sample-av-track-count" in repr(exc_info.value)


def test_av_track_count_multiple_video_is_typed_failure() -> None:
    """Two video tracks stop typed — the check never inspects only the
    first track of a kind."""
    ir = _full_ir()
    second_video = TimelineTrack0C(
        track=TrackRef0C(kind="video", index=4),
        items=(_item("v9", "video", 4, (0, 120), av_link="av9"),),
    )
    doubled = ir.model_copy(
        update={"tracks": (ir.tracks[0], second_video, ir.tracks[1], ir.tracks[2])}
    )
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(doubled, [_span(0, 30)])
    assert "sample-av-track-count" in repr(exc_info.value)


def test_av_track_count_multiple_audio_is_typed_failure() -> None:
    """Two audio tracks stop typed — same first-track-only closure."""
    ir = _full_ir()
    second_audio = TimelineTrack0C(
        track=TrackRef0C(kind="audio", index=5),
        items=(_item("a9", "audio", 5, (0, 120), av_link="av9"),),
    )
    doubled = ir.model_copy(
        update={"tracks": (ir.tracks[0], ir.tracks[1], second_audio, ir.tracks[2])}
    )
    with pytest.raises(PydanticCustomError) as exc_info:
        project_sample_ir(doubled, [_span(0, 30)])
    assert "sample-av-track-count" in repr(exc_info.value)


def test_derive_sample_windows_picks_three_positions_up_to_cap() -> None:
    """Head/middle/late POSITION sampling widened to contiguous ~8s
    spans (5-10s band), merged when they nearly touch, and capped to
    the limit — never a single short edit cut as-is (2026-09-10)."""
    video = TimelineTrack0C(
        track=TrackRef0C(kind="video", index=1),
        items=(
            _item("v1", "video", 1, (0, 150)),
            _item("v2", "video", 1, (600, 750)),
            _item("v3", "video", 1, (1200, 1350)),
        ),
    )
    ir = TimelineIr0C(
        artifact_id="ir-long-video-test",
        artifact_type="timeline_ir_0c",
        schema_version="timeline-ir-0c-v1",
        content_hash="e" * 64,
        producer=PRODUCER,
        inputs=(),
        rate=RATE,
        tracks=(video,),
    )
    windows = derive_sample_windows(ir, limit_seconds=30.0)
    assert len(windows) == 3  # head/middle/late anchors, far apart
    anchors = (0, 600, 1200)
    for window, anchor in zip(windows, anchors, strict=True):
        length = window.end_frame - window.start_frame
        assert 5 * 30 <= length <= 10 * 30  # contiguous 5-10s band
        assert window.start_frame <= anchor < window.end_frame
    assert sample_total_seconds(windows, RATE) <= 30.0
    # near-identical picks merge; the real (merged) count is the count
    merged = derive_sample_windows(_full_ir(), limit_seconds=30.0)
    assert len(merged) == 1  # head+middle anchors sit in one 4s span
    assert merged[0] == _span(0, 120)
    trimmed = derive_sample_windows(_full_ir(), limit_seconds=3.0)
    assert sample_total_seconds(trimmed, RATE) == pytest.approx(3.0)
    capped = derive_sample_windows(_full_ir(), limit_seconds=1.0)
    assert sample_total_seconds(capped, RATE) <= 1.0 + 1 / 30


def test_windows_around_anchors_matches_derive_for_same_anchors() -> None:
    """The shared widening base: GLM-observation candidate anchors widen
    exactly like the legacy position picks for identical anchors."""
    ir = _full_ir()
    assert windows_around_anchors(ir, [0, 60]) == derive_sample_windows(ir)
    with pytest.raises(PydanticCustomError) as exc_info:
        windows_around_anchors(ir, [])
    assert "sample-windows-empty" in repr(exc_info.value)
