"""Chapter-card media ops against the pinned ffmpeg on tiny synthetic media (TDD)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PIL import Image

import services.cli._v44_chapter_card_media as media_module
from services.cli._v44_chapter_card_media import (
    ChapterCardMediaError,
    FrameGeometry,
    FrameWindow,
    decode_pcm_s16le,
    dump_frames_raw,
    extract_frames,
    extract_selected_frames,
    frame_diff,
)
from services.cli._v44_chapter_card_probe import probe_master_facts
from services.cli._v44_chapter_card_render import (
    MasterGeometry,
    SpliceInputs,
    render_master,
    write_card_raw,
)
from tests.cli.v44_chapter_episode_workspace import pinned_tools_or_skip

if TYPE_CHECKING:
    from services.preview.tools import PinnedTools

FPS = 30
TOTAL_FRAMES = 60
RECORD_FRAME = 20
CARD_FRAMES = 9
SAMPLES_PER_FRAME = 1600
WIDTH = 320
HEIGHT = 180
CANVAS = FrameGeometry(WIDTH, HEIGHT)
SPAN = FrameWindow(CANVAS, start_frame=RECORD_FRAME, count=CARD_FRAMES)
GEOMETRY = MasterGeometry(
    width=320, height=180, fps=FPS, record_frame=RECORD_FRAME,
    card_frames=CARD_FRAMES, bitrate="4M", video_timescale=15360,
)


@pytest.fixture(scope="module")
def synthetic_master(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[PinnedTools, Path, bytes]:
    tools = pinned_tools_or_skip()
    work = tmp_path_factory.mktemp("chapter-card-media")
    source = work / "src.mp4"
    subprocess.run(
        (
            str(tools.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={TOTAL_FRAMES / FPS}",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-frames:v",
            str(TOTAL_FRAMES),
            "-c:v",
            "h264_videotoolbox",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-video_track_timescale",
            "15360",
            str(source),
        ),
        check=True,
        capture_output=True,
        timeout=300,
    )
    src_pcm = decode_pcm_s16le(tools, source)
    insert = RECORD_FRAME * SAMPLES_PER_FRAME * 4
    silence = CARD_FRAMES * SAMPLES_PER_FRAME * 4
    master_pcm = src_pcm[:insert] + b"\x00" * silence + src_pcm[insert:]
    pcm_path = work / "master.pcm"
    pcm_path.write_bytes(master_pcm)
    card_raw = work / "card.rgb"
    write_card_raw(
        Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0)), frames=CARD_FRAMES, path=card_raw
    )
    output = work / "master.mov"
    inputs = SpliceInputs(source=source, card_raw=card_raw, pcm=pcm_path, output=output)
    render_master(tools, inputs, GEOMETRY)
    return tools, output, master_pcm


def test_decode_pcm_s16le_returns_exact_sample_bytes(
    synthetic_master: tuple[PinnedTools, Path, bytes],
) -> None:
    # Given: the rendered synthetic master with 48kHz stereo PCM
    # When: decoding to canonical s16le
    # Then: exactly 69*1600 stereo sample frames come back byte-identical
    tools, output, master_pcm = synthetic_master
    assert decode_pcm_s16le(tools, output) == master_pcm
    assert len(master_pcm) == (TOTAL_FRAMES + CARD_FRAMES) * SAMPLES_PER_FRAME * 4


def test_master_probe_reports_spliced_geometry(
    synthetic_master: tuple[PinnedTools, Path, bytes],
) -> None:
    # Given: the rendered master MOV
    # When: probing with the pinned ffprobe
    # Then: 69 h264 frames at 30/1 on a 1/15360 time base, exact pcm samples
    tools, output, _ = synthetic_master
    facts = probe_master_facts(tools, output)
    assert facts.video_frames == TOTAL_FRAMES + CARD_FRAMES
    assert facts.avg_frame_rate == f"{FPS}/1"
    assert (facts.width, facts.height) == (WIDTH, HEIGHT)
    assert facts.video_codec == "h264"
    assert facts.video_time_base == "1/15360"
    assert facts.video_timescale == 15360
    assert facts.audio_codec == "pcm_s16le"
    assert facts.sample_rate == 48000
    assert facts.channels == 2
    assert facts.audio_samples == (TOTAL_FRAMES + CARD_FRAMES) * SAMPLES_PER_FRAME


def test_extract_frames_returns_contiguous_raw_rgb(
    synthetic_master: tuple[PinnedTools, Path, bytes],
) -> None:
    # Given: the rendered master
    # When: extracting the 9 card frames in one pass
    # Then: exactly 9 raw rgb24 frames, all near-black
    tools, output, _ = synthetic_master
    frames = extract_frames(tools, output, SPAN)
    assert len(frames) == CARD_FRAMES * WIDTH * HEIGHT * 3
    assert max(frames) <= 16


def test_extract_selected_frames_one_pinned_pass_per_media(
    synthetic_master: tuple[PinnedTools, Path, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the rendered master and a counting wrapper around the pinned runner
    # When: selecting sparse frames {0, 20, 68}
    # Then: exactly one subprocess returns every requested frame, keyed by index
    tools, output, _ = synthetic_master
    calls: list[tuple[str, ...]] = []
    real_run = media_module.run_pinned

    def _counting(tools: PinnedTools, argv: tuple[str, ...], *, timeout: int) -> bytes:
        calls.append(argv)
        return real_run(tools, argv, timeout=timeout)

    monkeypatch.setattr(media_module, "run_pinned", _counting)
    frames = media_module.extract_selected_frames(tools, output, CANVAS, (20, 0, 68, 20))
    assert len(calls) == 1
    assert set(frames) == {0, 20, 68}
    frame_bytes = WIDTH * HEIGHT * 3
    contiguous = extract_frames(tools, output, FrameWindow(CANVAS, 0, 69))
    assert frames[0] == contiguous[0:frame_bytes]
    assert frames[20] == contiguous[20 * frame_bytes : 21 * frame_bytes]
    assert frames[68] == contiguous[68 * frame_bytes :]


def test_extract_selected_frames_refuses_out_of_range(
    synthetic_master: tuple[PinnedTools, Path, bytes],
) -> None:
    # Given: a frame index beyond the stream
    # When: selecting it
    # Then: typed extract-length refusal
    tools, output, _ = synthetic_master
    with pytest.raises(ChapterCardMediaError, match="extract-length"):
        extract_selected_frames(tools, output, CANVAS, (0, 10_000))


def test_dump_frames_raw_streams_to_a_file(
    synthetic_master: tuple[PinnedTools, Path, bytes], tmp_path: Path
) -> None:
    # Given: the rendered master
    # When: dumping the card span to a raw file
    # Then: the file holds exactly the same pixels as the in-memory extraction
    tools, output, _ = synthetic_master
    raw = tmp_path / "span.raw"
    dump_frames_raw(tools, output, SPAN, raw)
    assert raw.stat().st_size == CARD_FRAMES * WIDTH * HEIGHT * 3
    assert raw.read_bytes() == extract_frames(tools, output, SPAN)


def test_frame_diff_counts_changed_pixels() -> None:
    # Given: two synthetic frames differing in one pixel channel
    # When: computing the diff at threshold 8
    # Then: max_abs, mean_abs, and mismatched fraction reflect that one pixel
    w, h = 4, 4
    base = bytearray(w * h * 3)
    changed = bytearray(base)
    changed[0] = 40
    diff = frame_diff(bytes(base), bytes(changed), FrameGeometry(w, h))
    assert diff.max_abs == 40
    assert diff.mean_abs == pytest.approx(40 / (w * h * 3))
    assert diff.mismatched_fraction == pytest.approx(1 / (w * h))
    assert frame_diff(bytes(base), bytes(base), FrameGeometry(w, h)).max_abs == 0


def test_render_master_refuses_missing_source(tmp_path: Path) -> None:
    # Given: no source media at all
    # When: rendering the master
    # Then: typed media refusal
    tools = pinned_tools_or_skip()
    inputs = SpliceInputs(
        source=tmp_path / "absent.mp4",
        card_raw=tmp_path / "card.rgb",
        pcm=tmp_path / "master.pcm",
        output=tmp_path / "out.mov",
    )
    with pytest.raises(ChapterCardMediaError):
        render_master(tools, inputs, GEOMETRY)
