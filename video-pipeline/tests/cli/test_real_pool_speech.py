"""speech_segments lattice behavior on REAL adjacent speech (PRD §2.5 fix).

Measured blocker (v44-real-01 arm A, round 2): 99 whisper segments of
back-to-back Japanese speech collided 73 times under the old ceil-end /
floor-start lattice quantization, and the final segment's ceil rounded
8467.44 -> 8469 past the 8467-frame source. The residue collapse keeps
every honest refusal: genuine overlaps beyond LATTICE frames and spans
that collapse inside the source stay typed errors.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Final

import pytest

from services.analyze.asr_models import (
    AsrInputBinding,
    AsrSettingsEcho,
    ExperimentalTiming,
    Producer,
    TranscriptArtifact,
    TranscriptSegment,
    WavProbeSummary,
    transcript_content_hash,
)
from services.cli.real_pool import RealPoolError, segments_from_ms, speech_segments

TOTAL_FRAMES: Final = 8467  # the real episode's verified mezzanine frames


def _transcript(spans_ms: tuple[tuple[int, int, str], ...]) -> TranscriptArtifact:
    segments = tuple(
        TranscriptSegment(start_ms=start, end_ms=end, text=text)
        for start, end, text in spans_ms
    )
    binding = AsrInputBinding(
        media_path="/synthetic/real.mp4",
        media_sha256="a" * 64,
        wav_sha256="b" * 64,
        wav_probe=WavProbeSummary(),
    )
    experimental = ExperimentalTiming(
        label="experimental-token-timing", token_level=None, consistent_with_segments=True
    )
    echo = AsrSettingsEcho(
        ffmpeg_argv=("/synthetic/bin/ffmpeg", "-nostdin"),
        whisper_argv=("/synthetic/bin/whisper-cli", "--model", "/synthetic/models/ggml.bin"),
    )
    content_hash = transcript_content_hash(binding, segments, experimental, echo)
    return TranscriptArtifact(
        artifact_id=f"transcript-{content_hash[:16]}",
        artifact_type="transcript_asr_whisper_cpp",
        schema_version="asr-transcript-v1",
        content_hash=content_hash,
        producer=Producer(name="asr-whisper-cpp", version="todo33-v1"),
        inputs=(),
        input_binding=binding,
        segments=segments,
        experimental=experimental,
        settings_echo=echo,
    )


def test_adjacent_real_speech_segments_share_boundaries_without_collision() -> None:
    """Given: two synthetic spans sharing a non-lattice-aligned ms boundary
    (floor-start 36 < ceil-end 39 under the old math); Then: the residue
    collapses to contiguity — segment 2 starts exactly at segment 1's end."""

    speech = speech_segments(
        _transcript(((120, 1234, "合成台本一号"), (1234, 2468, "合成台本二号"))),
        TOTAL_FRAMES,
    )

    assert len(speech) == 2
    assert speech[0].end_frame == speech[1].start_frame  # contiguous, no overlap
    assert speech[0].start_frame == 3
    assert speech[1].text == "合成台本二号"


def test_segments_from_ms_shares_the_transcript_lattice() -> None:
    """Task 3: the extracted ms-triples helper IS the speech_segments
    conversion — the corrected-sample path and the ASR path quantize through
    the identical deterministic lattice."""
    spans = ((120, 1234, "合成台本一号"), (1234, 2468, "合成台本二号"))

    direct = segments_from_ms(spans, TOTAL_FRAMES)
    via_transcript = speech_segments(_transcript(spans), TOTAL_FRAMES)

    assert direct == via_transcript


def test_segments_from_ms_preserves_the_overlap_refusal_directly() -> None:
    with pytest.raises(RealPoolError, match="overlaps the previous segment"):
        segments_from_ms(((0, 3000, "一本目"), (1500, 6000, "重複")), TOTAL_FRAMES)


def test_full_real_segment_chain_is_fully_contiguous() -> None:
    """Given: a long chain of back-to-back spans (the collision pattern
    measured 73/99 on the real episode); Then: every adjacent pair meets
    exactly (start == previous end) and spans stay inside the source."""

    boundaries = tuple(i * 730 for i in range(41))  # shared, non-lattice-aligned
    spans = tuple(
        (boundaries[i], boundaries[i + 1], f"セグメント{i}") for i in range(len(boundaries) - 1)
    )
    speech = speech_segments(_transcript(spans), TOTAL_FRAMES)

    for previous, current in pairwise(speech):
        assert current.start_frame == previous.end_frame  # every boundary shared
        assert current.start_frame < current.end_frame


def test_lattice_tail_overrun_clamps_to_the_source_bound() -> None:
    """Given: a synthetic tail span [280000, 282300) ms whose ceil-lattice
    end (8469) passes the 8467-frame source by 2 frames of pure rounding
    (the overrun shape measured on the real episode's final segment);
    Then: it clamps to the source bound instead of refusing the episode."""

    speech = speech_segments(
        _transcript(((280000, 282300, "合成台本ラスト"),)), TOTAL_FRAMES
    )

    assert speech[-1].end_frame == TOTAL_FRAMES == 8467


def test_genuine_overlap_beyond_the_residue_is_still_typed() -> None:
    """Given: a span overlapping the previous by seconds (hallucination
    class, far beyond LATTICE); Then: the typed refusal survives."""

    with pytest.raises(RealPoolError, match="overlaps the previous segment"):
        speech_segments(
            _transcript(((0, 3000, "一本目"), (1500, 6000, "重複"))), TOTAL_FRAMES
        )


def test_span_far_past_the_source_is_still_typed() -> None:
    """Given: a span claiming hundreds of frames past EOF; Then: the typed
    refusal survives (only lattice residue may clamp)."""

    with pytest.raises(RealPoolError, match="exceeds the edit source"):
        speech_segments(
            _transcript(((0, 3000, "前半"), (3000, 320_000, "暴走"))), TOTAL_FRAMES
        )
