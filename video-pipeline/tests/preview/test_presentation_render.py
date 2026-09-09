"""Presentation render settings: argv knobs and model guards (hermetic).

No ffmpeg runs: ``build_render_command`` is pure argv assembly, and the
models validate in-process. Given/When/Then per behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.timeline_ir import TimelineItem0C
from services.preview.ffmpeg_cmd import build_render_command
from services.preview.models import (
    ItemBinding,
    MediaBinding,
    PresentationRenderSettings,
    PreviewLayout,
    PreviewMediaBindings,
    TracePresentation,
)
from services.preview.tools import PinnedTools

RATE = RationalFrameRate(num=30, den=1)
FAKE_SHA = "ab" * 32


def _tools(root: Path) -> PinnedTools:
    return PinnedTools(
        ffmpeg=root / "ffmpeg",
        ffprobe=root / "ffprobe",
        ffmpeg_sha256="0" * 64,
        ffprobe_sha256="0" * 64,
    )


def _item(item_id: str, kind: str, start: int, end: int) -> TimelineItem0C:
    return TimelineItem0C(
        item_id=item_id,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        source=SourceRef(
            source_id="edit-source",
            span=SourceFrameSpan(start_frame=start, end_frame=end, rate=RATE),
        ),
        record_span=RecordFrameSpan(start_frame=start, end_frame=end),
    )


def _layout() -> PreviewLayout:
    return PreviewLayout(
        rate=RATE,
        video_items=(_item("v1", "video", 0, 30),),
        audio_items=(_item("a1", "audio", 0, 30),),
        subtitle_items=(),
        total_record_frames=30,
        samples_per_frame=1600,
    )


def _bindings(root: Path, *, with_bgm: bool) -> PreviewMediaBindings:
    media = str(root / "a.mov")
    return PreviewMediaBindings(
        items=(
            ItemBinding(
                item_id="v1",
                binding=MediaBinding(media_path=media, sha256=FAKE_SHA),
            ),
            ItemBinding(
                item_id="a1",
                binding=MediaBinding(media_path=media, sha256=FAKE_SHA),
            ),
        ),
        bgm=(
            MediaBinding(media_path=str(root / "bgm.wav"), sha256=FAKE_SHA)
            if with_bgm
            else None
        ),
    )


def test_plain_rebuild_argv_is_byte_identical(tmp_path: Path) -> None:
    """Given no presentation settings, When argv is assembled with the
    default vs an explicit None gain, Then the bytes are identical."""

    layout, tools = _layout(), _tools(tmp_path)
    output = tmp_path / "out.mp4"
    default = build_render_command(
        layout=layout, bindings=_bindings(tmp_path, with_bgm=True), tools=tools,
        output=output, subtitle_srt=None,
    )
    explicit_none = build_render_command(
        layout=layout, bindings=_bindings(tmp_path, with_bgm=True), tools=tools,
        output=output, subtitle_srt=None, bgm_gain_mb=None,
    )
    assert default.argv == explicit_none.argv
    assert default.filter_complex == explicit_none.filter_complex


def test_bgm_gain_applies_a_real_volume_filter(tmp_path: Path) -> None:
    """Given a bound BGM and a -600 mB override, When argv is assembled,
    Then the BGM branch carries the volume cut."""

    command = build_render_command(
        layout=_layout(), bindings=_bindings(tmp_path, with_bgm=True),
        tools=_tools(tmp_path), output=tmp_path / "out.mp4", subtitle_srt=None,
        bgm_gain_mb=-600,
    )
    assert "volume=-0.600dB" in command.filter_complex


def test_bgm_gain_without_binding_changes_nothing(tmp_path: Path) -> None:
    """Given no BGM binding (review-plane path), When a gain override is
    present, Then argv is identical — no fake effect is rendered."""

    layout, tools = _layout(), _tools(tmp_path)
    output = tmp_path / "out.mp4"
    plain = build_render_command(
        layout=layout, bindings=_bindings(tmp_path, with_bgm=False), tools=tools,
        output=output, subtitle_srt=None,
    )
    with_gain = build_render_command(
        layout=layout, bindings=_bindings(tmp_path, with_bgm=False), tools=tools,
        output=output, subtitle_srt=None, bgm_gain_mb=-600,
    )
    assert with_gain.argv == plain.argv


def test_empty_render_settings_are_rejected() -> None:
    """Given neither subtitle nor BGM override, When constructed, Then
    the model refuses (a missing object means a plain rebuild)."""

    with pytest.raises(ValidationError):
        PresentationRenderSettings()


def test_trace_presentation_defaults_to_absent() -> None:
    """Given a trace record without presentation, When validated, Then
    the field defaults to None (old manifests keep parsing)."""

    trace = TracePresentation(applied_command_ids=())
    assert trace.subtitle_max_chars_per_line is None
    assert trace.bgm_gain_mb is None
    assert trace.notes == ()
