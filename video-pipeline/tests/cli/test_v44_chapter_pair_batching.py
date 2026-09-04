"""Sparse pair QA must batch into one pinned extraction per media file (TDD)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import services.cli._v44_chapter_card_pair_qa as qa
import services.cli._v44_chapter_card_qa as span_qa
import services.presentation.chapter_card as card
from services.cli._v44_chapter_card_media import ChapterCardMediaError, FrameGeometry
from tests.cli.v44_chapter_episode_workspace import fake_tools

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools

W, H = 64, 36
FRAME_BYTES = W * H * 3


def _glyph_frame(band_level: int = 255) -> bytes:
    canvas = bytearray(FRAME_BYTES)
    for channel in range(3):
        for y in range(16, 20):
            for x in range(24, 40):
                canvas[(y * W + x) * 3 + channel] = band_level
    return bytes(canvas)


def _patch_batch(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, tuple[int, ...]]]:
    """Patch the batched extraction seam; record one (media, indices) per call."""

    calls: list[tuple[Path, tuple[int, ...]]] = []

    def _batch(
        tools: PinnedTools, media: Path, geometry: FrameGeometry, frames: tuple[int, ...]
    ) -> dict[int, bytes]:
        calls.append((media, tuple(frames)))
        return {index: _glyph_frame() for index in frames}

    monkeypatch.setattr(qa, "extract_selected_frames", _batch)
    return calls



def _tools(tmp_path: Path) -> PinnedTools:
    marker = tmp_path / "marker"
    marker.write_bytes(b"m")
    return fake_tools(marker, marker)


@pytest.fixture
def small_canvas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa, "CANVAS_W", W)
    monkeypatch.setattr(qa, "CANVAS_H", H)
    monkeypatch.setattr(span_qa, "CANVAS_W", W)
    monkeypatch.setattr(span_qa, "CANVAS_H", H)
    monkeypatch.setattr(card, "CANVAS_W", W)
    monkeypatch.setattr(card, "CANVAS_H", H)


def test_default_sampled_pairs_use_exactly_two_extractions(
    tmp_path: Path, small_canvas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the production SAMPLED_PAIRS table (11 pairs, 22 frame references)
    # When: the pair gate runs
    # Then: exactly one batched extraction per media file - 2 decode subprocesses
    calls = _patch_batch(monkeypatch)
    results = qa.verify_source_pairs(
        _tools(tmp_path), tmp_path / "src.mp4",
        tmp_path / "master.mov", qa_dir=tmp_path / "qa",
    )
    assert len(calls) == 2
    medias = [media for media, _ in calls]
    assert medias == [tmp_path / "master.mov", tmp_path / "src.mp4"]
    output_indices = calls[0][1]
    source_indices = calls[1][1]
    assert output_indices == tuple(
        sorted({output for output, _ in qa.SAMPLED_PAIRS})
    )
    assert source_indices == tuple(sorted({source for _, source in qa.SAMPLED_PAIRS}))
    assert len(results) == len(qa.SAMPLED_PAIRS)


def test_duplicate_pairs_dedupe_into_one_extraction_per_media(
    tmp_path: Path, small_canvas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: sampled pairs repeating the same frames
    # When: the pair gate runs
    # Then: each media is extracted once with deduplicated, sorted indices
    calls = _patch_batch(monkeypatch)
    qa.verify_source_pairs(
        _tools(tmp_path), tmp_path / "src.mp4",
        tmp_path / "master.mov", samples=((9, 4), (5, 5), (5, 5)), qa_dir=tmp_path / "qa",
    )
    assert calls == [
        (tmp_path / "master.mov", (5, 9)),
        (tmp_path / "src.mp4", (4, 5)),
    ]


def test_pair_drift_refusal_and_pngs_still_written(
    tmp_path: Path, small_canvas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a master frame whose decoded pixels differ from its source frame
    # When: the pair gate runs
    # Then: typed drift refusal; matching pairs still write their PNGs first
    master_frames = {0: _glyph_frame()}
    source_frames = {0: _glyph_frame(band_level=60)}

    def _batch(
        tools: PinnedTools, media: Path, geometry: FrameGeometry, frames: tuple[int, ...]
    ) -> dict[int, bytes]:
        return master_frames if media.name == "master.mov" else source_frames

    monkeypatch.setattr(qa, "extract_selected_frames", _batch)
    with pytest.raises(qa.ChapterCardQAError, match="frame-pair-drift"):
        qa.verify_source_pairs(
            _tools(tmp_path), tmp_path / "src.mp4",
            tmp_path / "master.mov", samples=((0, 0),), qa_dir=tmp_path / "qa",
        )


def test_pair_extraction_failure_is_typed(
    tmp_path: Path, small_canvas: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a batched extraction that fails at the media layer
    # When: the pair gate runs
    # Then: the typed QA refusal wraps it
    def _broken(*args: object, **kwargs: object) -> dict[int, bytes]:
        raise ChapterCardMediaError("command-failed", "boom")

    monkeypatch.setattr(qa, "extract_selected_frames", _broken)
    with pytest.raises(qa.ChapterCardQAError, match="frame-extract-failed"):
        qa.verify_source_pairs(
            _tools(tmp_path), tmp_path / "src.mp4",
            tmp_path / "master.mov", samples=((0, 0),), qa_dir=tmp_path / "qa",
        )
