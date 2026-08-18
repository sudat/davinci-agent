"""Todo 44 acceptance: subtitle cue generation over the frozen Japanese rules.

Dialogue filtering (removed spans get no cues), edit-boundary splitting at
exact integer frames, frozen Japanese punctuation/meaning formatting, and the
declared min-duration action are all exercised against the frozen goldens.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.compile.ir_qc import IrQcError
from services.compile.subtitle_cues import format_cue
from services.compile.subtitle_policy import (
    FrameCueSpan,
    SubtitleQcPolicy,
    TranscriptCueSegment,
    TranscriptCueSource,
)

from .support import (
    compile_fixture,
    default_policy,
    golden_fixture,
    golden_transcript_cue_source,
    manifest_cue_source,
)

REF01 = "p1-ref-01-clean-ja"
REF02 = "p1-ref-02-pauses-fillers"
REF05 = "p1-ref-05-review-mix"
SUBTITLE_TRACK = 3


def _cue_rows(fixture_compile):
    return [
        (
            item.source.span.start_frame,
            item.source.span.end_frame,
            item.record_span.start_frame,
            item.record_span.end_frame,
            item.text,
        )
        for track in fixture_compile.ir.tracks
        if track.track.index == SUBTITLE_TRACK
        for item in track.items
    ]


def _cue_source(spans: tuple[tuple[str, str, int, int], ...]) -> TranscriptCueSource:
    return TranscriptCueSource(
        language="ja",
        segments=tuple(
            TranscriptCueSegment(
                segment_id=segment_id,
                text=text,
                span=FrameCueSpan(start_frame=start, end_frame=end),
            )
            for segment_id, text, start, end in spans
        ),
    )


# ---------------------------------------------------- dialogue filtering


def test_dialogue_filtering_removed_spans_get_no_cues() -> None:
    """REF02 drops f1/p2/s3/p3; only cues over kept spans survive."""

    transcript = golden_transcript_cue_source(REF02, ("s1", "s2", "s3", "s4", "s5", "f1"))

    result = compile_fixture(REF02, transcript=transcript)

    rows = _cue_rows(result)
    # s3 (359-509) and f1 (314-344) were removed by the plan; s2 is kept.
    assert [row[4] for row in rows] == [
        "今日は撮影の裏側をお見せします。",
        "まずカメラのセッティングからです。",
        "それでは本編を始めます。",
        "撮影のメイキングをお楽しみください。",
    ]
    assert all(not (row[0] >= 314 and row[1] <= 344) for row in rows), "no cue over f1"
    assert all(not (row[0] >= 359 and row[1] <= 509) for row in rows), "no cue over removed s3"
    plan_items = golden_fixture(REF02)["plan_items"]
    assert isinstance(plan_items, list)
    expected_records = [
        (item["span"]["start_frame"], item["span"]["end_frame"])  # type: ignore[index]
        for item in plan_items
        if isinstance(item, dict)
        and item["kind"] == "video"
        and item["segment_id"] in {"s1", "s2", "s4", "s5"}
    ]
    assert [(row[2], row[3]) for row in rows] == expected_records


# ------------------------------------------------ edit-boundary splitting


def test_edit_boundary_splitting_at_exact_frames() -> None:
    crossing = _cue_source((("s1", "境界をまたぐ長いセリフです。", 100, 460),))

    result = compile_fixture(REF01, transcript=crossing)

    assert _cue_rows(result) == [
        (100, 150, 100, 150, "境界をまたぐ長いセリフです。"),
        (150, 300, 150, 300, "境界をまたぐ長いセリフです。"),
        (300, 450, 300, 450, "境界をまたぐ長いセリフです。"),
    ]
    assert len(result.dropped_cues) == 1, "the 10-frame tail sliver drops below min"


def test_split_pieces_carry_the_source_text_per_frozen_rule() -> None:
    crossing = _cue_source((("s1", "完全な一文を保持します。", 100, 460),))

    result = compile_fixture(REF01, transcript=crossing)

    texts = [row[4] for row in _cue_rows(result)]
    assert texts == ["完全な一文を保持します。"] * 3


# -------------------------------------------------- min duration (frozen)


def test_min_duration_pieces_are_dropped_deterministically() -> None:
    sliver = _cue_source((("s1", "短すぎる断片。", 149, 151),))

    result = compile_fixture(REF01, transcript=sliver)

    assert _cue_rows(result) == []
    assert len(result.dropped_cues) == 2
    again = compile_fixture(REF01, transcript=sliver)
    assert again.dropped_cues == result.dropped_cues


def test_cue_exactly_at_min_duration_is_kept() -> None:
    minimal = _cue_source((("s1", "最低時間ちょうど。", 135, 150),))

    result = compile_fixture(REF01, transcript=minimal)

    assert _cue_rows(result) == [
        (135, 150, 135, 150, "最低時間ちょうど。")
    ]


# ------------------------------------------------ Japanese formatting rules


def test_trailing_clause_mark_is_stripped_and_sentence_mark_kept() -> None:
    policy = default_policy()

    stripped = format_cue("えっと、", policy)
    kept = format_cue("はい、そうです。", policy)

    assert stripped is not None
    assert stripped.text == "えっと"
    assert kept is not None
    assert kept.text == "はい、そうです。"


def test_meaning_boundary_line_breaking() -> None:
    policy = default_policy()

    formatted = format_cue(
        "まず素材の取り込みから始めましょう。次にタイムラインの編集方法を説明します。",
        policy,
    )

    assert formatted is not None
    assert formatted.lines == (
        "まず素材の取り込みから始めましょう。",
        "次にタイムラインの編集方法を説明します。",
    )
    assert formatted.safe_area is True


def test_line_break_never_leaves_a_trailing_clause_mark() -> None:
    policy = default_policy()

    formatted = format_cue("今日はもう遅いので、ここまでにしましょう。", policy)

    assert formatted is not None
    assert all(not line.endswith("、") for line in formatted.lines)
    assert "".join(formatted.lines).replace("、", "") == formatted.text.replace("、", "")


def test_unsafe_layout_fails_closed_at_compile() -> None:
    unbreakable = _cue_source(
        (("s1", "この文は意味境界なしで非常に長く続くため安全領域に収まりません", 0, 150),)
    )

    with pytest.raises(IrQcError) as error:
        compile_fixture(REF01, transcript=unbreakable)
    rules = [v.rule_id for v in error.value.violations]
    assert "subtitle_safe_area" in rules


def test_style_refs_cannot_be_invented() -> None:
    with pytest.raises(ValidationError, match="style_ref"):
        SubtitleQcPolicy(
            policy_id="bad-policy",
            min_duration_frames=15,
            max_lines=2,
            max_chars_per_line=18,
            declared_style_refs=("style-a",),
            default_style_ref="style-invented",
        )


def test_cues_declared_in_the_frozen_manifest_compile_unchanged() -> None:
    """REF05 manifest texts round-trip byte-identically through formatting."""

    result = compile_fixture(REF05)

    declared = [s.text for s in manifest_cue_source(REF05).segments]
    assert [row[4] for row in _cue_rows(result)] == declared


def test_compile_without_cues_emits_no_subtitle_track() -> None:
    empty = TranscriptCueSource(language="ja", segments=())

    result = compile_fixture(REF01, transcript=empty)

    assert all(track.track.index != SUBTITLE_TRACK for track in result.ir.tracks)
