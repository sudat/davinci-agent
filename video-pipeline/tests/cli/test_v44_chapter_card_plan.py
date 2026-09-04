"""Chapter-card insertion plan math, PCM assembly, and proposal gate (TDD)."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.cli._v44_chapter_card_plan import (
    CANDIDATE_ID,
    CARD_FRAMES,
    INSERT_SAMPLE,
    MODEL_ID,
    OPERATOR_AUDIO_STATEMENT,
    OPERATOR_TITLE_STATEMENTS,
    OUTPUT_FRAMES,
    OUTPUT_PCM_BYTES,
    OUTPUT_SAMPLES,
    PREFIX_BYTES,
    RECORD_FRAME,
    SILENCE_BYTES,
    SILENCE_SAMPLES,
    SOURCE_FRAMES,
    SOURCE_PCM_BYTES,
    SOURCE_PCM_SHA256,
    SOURCE_SAMPLES,
    TITLE,
    ChapterCardPlanError,
    build_output_pcm,
    frame_kind,
    load_approved_card,
    source_frame_for_output,
    split_output_pcm,
)
from tests.cli.v44_chapter_sidecar_factory import write_sidecar


def _write_sidecar(tmp_path: Path, mutate: str, value: object) -> Path:
    return write_sidecar(tmp_path, mutate=mutate, value=value)


def test_frame_and_sample_math_is_self_consistent() -> None:
    # Given: the approved boundary constants
    # When: deriving counts
    # Then: every count composes from 30fps video and 48kHz stereo audio
    assert (SOURCE_FRAMES, RECORD_FRAME, CARD_FRAMES, OUTPUT_FRAMES) == (7792, 1632, 45, 7837)
    assert OUTPUT_FRAMES == SOURCE_FRAMES + CARD_FRAMES
    assert INSERT_SAMPLE == RECORD_FRAME * 1600 == 2_611_200
    assert SILENCE_SAMPLES == CARD_FRAMES * 1600 == 72_000
    assert OUTPUT_SAMPLES == SOURCE_SAMPLES + SILENCE_SAMPLES == 12_539_136
    assert PREFIX_BYTES == INSERT_SAMPLE * 4 == 10_444_800
    assert SILENCE_BYTES == SILENCE_SAMPLES * 4 == 288_000
    assert SOURCE_PCM_BYTES == SOURCE_SAMPLES * 4 == 49_868_544
    assert OUTPUT_PCM_BYTES == OUTPUT_SAMPLES * 4 == 50_156_544
    assert len(SOURCE_PCM_SHA256) == 64


def test_build_output_pcm_splices_exact_silence() -> None:
    # Given: canonical source PCM of the exact expected byte length
    # When: building the master PCM
    # Then: prefix bytes, 288,000 zero bytes, then the full source suffix
    marker = bytes(range(256)) * (SOURCE_PCM_BYTES // 256)
    source = marker[:SOURCE_PCM_BYTES]
    built = build_output_pcm(source)
    assert len(built) == OUTPUT_PCM_BYTES
    assert built[:PREFIX_BYTES] == source[:PREFIX_BYTES]
    assert built[PREFIX_BYTES : PREFIX_BYTES + SILENCE_BYTES] == b"\x00" * SILENCE_BYTES
    assert built[PREFIX_BYTES + SILENCE_BYTES :] == source[PREFIX_BYTES:]


def test_build_output_pcm_refuses_wrong_length() -> None:
    # Given: source PCM one byte short
    # When: building the master PCM
    # Then: typed refusal naming the length gate
    with pytest.raises(ChapterCardPlanError, match="source-pcm-length"):
        build_output_pcm(b"\x00" * (SOURCE_PCM_BYTES - 1))


def test_split_output_pcm_returns_three_exact_parts() -> None:
    # Given: constructed master PCM with distinctive suffix
    # When: splitting into proof parts
    # Then: lengths and content match the splice plan
    source = bytearray(SOURCE_PCM_BYTES)
    source[PREFIX_BYTES:] = b"\xab" * (SOURCE_PCM_BYTES - PREFIX_BYTES)
    built = build_output_pcm(bytes(source))
    prefix, silence, suffix = split_output_pcm(built)
    assert (len(prefix), len(silence), len(suffix)) == (
        PREFIX_BYTES,
        SILENCE_BYTES,
        SOURCE_PCM_BYTES - PREFIX_BYTES,
    )
    assert silence == b"\x00" * SILENCE_BYTES
    assert suffix == b"\xab" * (SOURCE_PCM_BYTES - PREFIX_BYTES)
    with pytest.raises(ChapterCardPlanError, match="output-pcm-length"):
        split_output_pcm(built[:-1])


def test_frame_mapping_covers_every_output_frame() -> None:
    # Given: the approved insertion boundary
    # When: mapping output frames back to source frames
    # Then: identity before the card, card span, then a uniform 45-frame shift
    assert source_frame_for_output(0) == 0
    assert source_frame_for_output(RECORD_FRAME - 1) == 1631
    assert source_frame_for_output(RECORD_FRAME) is None
    assert source_frame_for_output(RECORD_FRAME + CARD_FRAMES - 1) is None
    assert source_frame_for_output(RECORD_FRAME + CARD_FRAMES) == 1632
    assert source_frame_for_output(OUTPUT_FRAMES - 1) == 7791
    assert frame_kind(0) == "source"
    assert frame_kind(RECORD_FRAME) == "card"
    assert frame_kind(RECORD_FRAME + CARD_FRAMES) == "source"


def test_load_approved_card_accepts_the_approved_sidecar(tmp_path: Path) -> None:
    # Given: the approved runtime sidecar
    # When: loading with the frozen expectations
    # Then: the bound card facts come back intact
    card = load_approved_card(_write_sidecar(tmp_path, "none", None))
    assert card.title == TITLE
    assert card.candidate_id == CANDIDATE_ID
    assert card.model_id == MODEL_ID
    assert card.record_frame == RECORD_FRAME
    assert card.card_frames == CARD_FRAMES
    assert card.source_frame == 1845
    assert card.left_item_id == "s20"
    assert card.right_item_id == "s21"


@pytest.mark.parametrize(
    ("mutate", "value"),
    [
        ("candidate_id", "f" * 64),
        ("title", "別のタイトル"),
        ("approval_status", "approved"),
        ("proposal_only", False),
        ("chapter_card_duration_frames", 30),
    ],
)
def test_load_approved_card_refuses_drift(tmp_path: Path, mutate: str, value: object) -> None:
    # Given: a sidecar that drifted from the approved selection
    # When: loading with the frozen expectations
    # Then: typed refusal (no render may proceed from an unapproved proposal)
    with pytest.raises(ChapterCardPlanError):
        load_approved_card(_write_sidecar(tmp_path, mutate, value))


def test_operator_statements_record_bag_case_disclosure() -> None:
    # Given: the provenance constants
    # Then: the recorded statements disclose the paraphrase, the theme wording,
    # the informed operator selection, and the exact audio change
    joined = " ".join(OPERATOR_TITLE_STATEMENTS)
    assert "どこにもないカメラバッグ" in joined
    assert "バッグ" in joined
    assert "ケース" in joined
    assert OPERATOR_AUDIO_STATEMENT == (
        "オペレーター承認のうえで1.5秒の無音を挿入。それ以外の音声改変なし"
    )
