"""Card-span QA streams frames from a raw file - never a whole-span blob (TDD)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import services.cli._v44_chapter_card_media as media_module
import services.cli._v44_chapter_card_qa as qa
import services.presentation.chapter_card as card
from services.cli._v44_chapter_card_media import ChapterCardMediaError, FrameWindow
from tests.cli.v44_chapter_episode_workspace import fake_tools

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools

W, H = 64, 36
FRAME_BYTES = W * H * 3
CARD_FRAMES = 45
BAND = (24, 16, 40, 20)


def _frame(
    *, band_level: int = 255, background: int = 0, band_shift: int = 0,
) -> bytes:
    canvas = bytearray(background.to_bytes(1, "big") * FRAME_BYTES)
    for channel in range(3):
        for y in range(BAND[1], BAND[3]):
            for x in range(BAND[0] + band_shift, BAND[2] + band_shift):
                canvas[(y * W + x) * 3 + channel] = band_level
    return bytes(canvas)


def _span_file(frames: list[bytes]) -> bytes:
    return b"".join(frames)



def _tools(tmp_path: Path) -> PinnedTools:
    marker = tmp_path / "marker"
    marker.write_bytes(b"m")
    return fake_tools(marker, marker)


@pytest.fixture
def small_canvas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa, "CANVAS_W", W)
    monkeypatch.setattr(qa, "CANVAS_H", H)
    monkeypatch.setattr(qa, "FRAME_BYTES", FRAME_BYTES)
    monkeypatch.setattr(qa, "CARD_PNG_SAMPLES", (0, 22, 44))
    monkeypatch.setattr(card, "CANVAS_W", W)
    monkeypatch.setattr(card, "CANVAS_H", H)


def _patch_dump(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> list[tuple[FrameWindow, Path]]:
    calls: list[tuple[FrameWindow, Path]] = []

    def _dump(
        tools: PinnedTools, media: Path, window: FrameWindow, destination: Path
    ) -> None:
        calls.append((window, destination))
        destination.write_bytes(payload)

    monkeypatch.setattr(qa, "dump_frames_raw", _dump)
    return calls


def test_card_span_streams_from_raw_file_never_a_blob(
    tmp_path: Path, small_canvas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a happy 45-frame card span delivered through the raw-file seam
    # When: the card-span gate runs
    # Then: one dump call, zero blob extractions, all checks enforced, temp removed
    def _no_blob(*args: object, **kwargs: object) -> bytes:
        message = "whole-span blob extraction must not be used for card QA"
        raise AssertionError(message)

    monkeypatch.setattr(media_module, "extract_frames", _no_blob)
    calls = _patch_dump(monkeypatch, _span_file([_frame() for _ in range(CARD_FRAMES)]))
    report = qa.verify_card_span(
        _tools(tmp_path), tmp_path / "master.mov",
        record_frame=0, card_frames=CARD_FRAMES, qa_dir=tmp_path / "qa",
    )
    assert len(calls) == 1
    window, destination = calls[0]
    assert (window.start_frame, window.count) == (0, CARD_FRAMES)
    assert (window.geometry.width, window.geometry.height) == (W, H)
    assert destination.parent == tmp_path / "qa"
    assert report["card_frames"] == CARD_FRAMES
    assert report["per_frame_white_min"] == 255
    assert report["background_max"] == 0
    assert report["max_neighbor_diff"] == 0
    assert not list((tmp_path / "qa").glob("*.raw")), "temporary raw span file must be removed"
    assert (tmp_path / "qa" / "card-first.png").is_file()


def test_card_span_short_raw_file_is_typed(
    tmp_path: Path, small_canvas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a raw span file shorter than the requested frame count
    # When: the card-span gate iterates it
    # Then: typed refusal and the temporary file is still cleaned up
    _patch_dump(monkeypatch, _span_file([_frame() for _ in range(3)]))
    with pytest.raises(qa.ChapterCardQAError, match="card-extract-failed"):
        qa.verify_card_span(
            _tools(tmp_path), tmp_path / "master.mov",
            record_frame=0, card_frames=CARD_FRAMES, qa_dir=tmp_path / "qa",
        )
    assert not list((tmp_path / "qa").glob("*.raw"))


def test_card_span_streaming_keeps_every_per_frame_gate(
    tmp_path: Path, small_canvas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: streamed spans with a dim frame, a gray background, and a moving glyph
    # When: the card-span gate iterates them
    # Then: each failure mode is still refused from the streaming path
    dim = [_frame() for _ in range(CARD_FRAMES)]
    dim[30] = _frame(band_level=120)
    _patch_dump(monkeypatch, _span_file(dim))
    with pytest.raises(qa.ChapterCardQAError, match="card-frame-dim"):
        qa.verify_card_span(
            _tools(tmp_path), tmp_path / "master.mov",
            record_frame=0, card_frames=CARD_FRAMES, qa_dir=tmp_path / "qa",
        )

    _patch_dump(monkeypatch, _span_file([_frame(background=60) for _ in range(CARD_FRAMES)]))
    with pytest.raises(qa.ChapterCardQAError, match="card-background-not-black"):
        qa.verify_card_span(
            _tools(tmp_path), tmp_path / "master.mov",
            record_frame=0, card_frames=CARD_FRAMES, qa_dir=tmp_path / "qa",
        )

    moving = [_frame() for _ in range(CARD_FRAMES)]
    moving[22] = _frame(band_shift=6)
    _patch_dump(monkeypatch, _span_file(moving))
    with pytest.raises(qa.ChapterCardQAError, match="card-frame-changing"):
        qa.verify_card_span(
            _tools(tmp_path), tmp_path / "master.mov",
            record_frame=0, card_frames=CARD_FRAMES, qa_dir=tmp_path / "qa",
        )


def test_card_span_dump_failure_is_typed(
    tmp_path: Path, small_canvas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a raw dump that fails at the media layer
    # When: the card-span gate runs
    # Then: typed refusal and no temporary file remains
    def _broken(*args: object, **kwargs: object) -> None:
        raise ChapterCardMediaError("command-failed", "boom")

    monkeypatch.setattr(qa, "dump_frames_raw", _broken)
    with pytest.raises(qa.ChapterCardQAError, match="card-extract-failed"):
        qa.verify_card_span(
            _tools(tmp_path), tmp_path / "master.mov",
            record_frame=0, card_frames=CARD_FRAMES, qa_dir=tmp_path / "qa",
        )
    assert not list((tmp_path / "qa").glob("*.raw"))
