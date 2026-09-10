"""Subtitle 4-choice confirmation + presentation_overrides concept split.

Hermetic (no ffmpeg, no LLM). Given/When/Then per behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.compile.subtitle_policy import (
    SUBTITLE_BASE_CHARS_PER_LINE,
    SUBTITLE_MIN_CHARS_PER_LINE,
    SUBTITLE_NARROWING_KINDS,
    SUBTITLE_SHORTER_STEP_CHARS,
    subtitle_wrap_widths,
)
from services.episode_cockpit.presentation_overrides import (
    PresentationTranslationError,
    derive_presentation_overrides,
)
from services.episode_cockpit.review_apply import apply_drafts
from services.episode_cockpit.review_chat import (
    SUBTITLE_AMBIGUOUS_QUESTION,
    SUBTITLE_CHOICE_DURATION,
    SUBTITLE_CHOICE_SPLIT,
    SUBTITLE_CHOICE_SUMMARY,
    SUBTITLE_CHOICE_WRAP,
    SUBTITLE_NO_KNOB_REFUSAL_DETAIL,
    SUBTITLE_SUMMARY_ACK_REQUEST,
    AppliedCommand,
    ReviewChatContext,
    ReviewChatError,
    ReviewCommandDraft,
    ReviewCommandKind,
    ReviewStoreLocation,
    apply_command,
    interpret_command,
    validate_draft_applicable,
)
from services.review_command.store import initialize_store, load_head
from tests.review_command.support import manifest_plan

FOUR_CHOICES = (
    SUBTITLE_CHOICE_SPLIT,
    SUBTITLE_CHOICE_DURATION,
    SUBTITLE_CHOICE_SUMMARY,
    SUBTITLE_CHOICE_WRAP,
)


def _applied(
    command_id: str, kind: ReviewCommandKind, *, explicit_ack: bool = False
) -> AppliedCommand:
    return AppliedCommand(
        command_id=command_id,
        command_kind=kind,
        affected_domain="presentation",  # type: ignore[arg-type]
        explicit_ack=explicit_ack,
    )


def _draft(
    kind: ReviewCommandKind, text: str, *, explicit_ack: bool = False
) -> ReviewCommandDraft:
    return ReviewCommandDraft(
        command_id="rcmd-testdraft01",
        command_kind=kind,
        text=text,
        needs_confirmation=False,
        explicit_ack=explicit_ack,
    )


def test_four_choice_strings_are_plain_japanese() -> None:
    """Given the 4-choice contract, When read, Then each string names one
    distinct fix in plain Japanese (the exact operator-facing wording)."""

    assert SUBTITLE_CHOICE_SPLIT == "言葉は変えず、一度に出す文字を少なく分ける"
    assert SUBTITLE_CHOICE_DURATION == "表示している時間を短くする"
    assert SUBTITLE_CHOICE_SUMMARY == (
        "話した内容を要約して文章自体を短くする"
        "（発話どおりではなくなる——明示了承が必要）"  # noqa: RUF001 (required JA wording)
    )
    assert SUBTITLE_CHOICE_WRAP == "一行の幅だけ狭くして折り返す"


@pytest.mark.parametrize("text", ["字幕を短くして", "make subtitles shorter"])
def test_ambiguous_subtitle_shorter_needs_confirmation_with_four_choices(
    text: str,
) -> None:
    """Given the ambiguous subtitle_shorter phrasing, When interpreted,
    Then the draft needs confirmation and carries the 4-choice question —
    never an auto-mapped wrap narrowing."""

    draft = interpret_command(text, ReviewChatContext(at_seconds=None))
    assert draft.command_kind == "subtitle_shorter"
    assert draft.needs_confirmation is True
    assert draft.confirmation_reason == SUBTITLE_AMBIGUOUS_QUESTION
    for choice in FOUR_CHOICES:
        assert choice in (draft.confirmation_reason or "")


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("字幕を分けて表示して", "split_display"),
        ("字幕の表示時間を短くして", "duration_shorten"),
        ("字幕を折り返して表示して", "line_wrap"),
        ("split the subtitles", "split_display"),
        ("shorten the subtitle display time", "duration_shorten"),
        ("wrap the subtitles", "line_wrap"),
    ],
)
def test_explicit_choices_parse_without_confirmation(
    text: str, kind: ReviewCommandKind
) -> None:
    """Given an explicit choice phrasing, When interpreted, Then the draft
    carries that distinct kind with no confirmation flag."""

    draft = interpret_command(text, ReviewChatContext(at_seconds=None))
    assert draft.command_kind == kind
    assert draft.needs_confirmation is False
    assert draft.confirmation_reason is None


def test_summary_without_ack_needs_confirmation() -> None:
    """Given a summary request without acknowledgment, When interpreted,
    Then the draft needs confirmation asking for the explicit ack."""

    draft = interpret_command("字幕を要約して", ReviewChatContext(at_seconds=None))
    assert draft.command_kind == "text_summary_ack"
    assert draft.needs_confirmation is True
    assert draft.confirmation_reason == SUBTITLE_SUMMARY_ACK_REQUEST
    assert draft.explicit_ack is False


@pytest.mark.parametrize("text", ["字幕を要約してよい", "字幕を要約します。了承済みです"])
def test_summary_with_ack_parses_without_confirmation(text: str) -> None:
    """Given a summary request with acknowledgment words, When interpreted,
    Then the draft is confirmed and carries the ack flag."""

    draft = interpret_command(text, ReviewChatContext(at_seconds=None))
    assert draft.command_kind == "text_summary_ack"
    assert draft.needs_confirmation is False
    assert draft.explicit_ack is True


def test_summary_apply_without_ack_refused(tmp_path: Path) -> None:
    """Given a confirmed-shape summary draft without the ack flag, When
    applied, Then the same honest not-implemented refusal is raised (an
    ack alone would change nothing — there is no summary body to write)."""

    store = ReviewStoreLocation(
        log_path=tmp_path / "events.jsonl", plan_dir=tmp_path / "store"
    )
    draft = _draft("text_summary_ack", "字幕を要約して", explicit_ack=False)
    with pytest.raises(ReviewChatError) as exc_info:
        apply_command(draft, store=store)
    assert exc_info.value.code == "subtitle-choice-not-implemented"
    assert exc_info.value.detail == SUBTITLE_NO_KNOB_REFUSAL_DETAIL


def test_line_wrap_maps_to_existing_wrap_override() -> None:
    """Given one applied line_wrap, When derived, Then the compile settings
    carry the same stepped width the legacy kind produced."""

    when = derive_presentation_overrides((_applied("rcmd-aaaa00000001", "line_wrap"),))
    assert len(when.overrides) == 1
    entry = when.overrides[0]
    assert entry.command_kind == "line_wrap"
    assert entry.setting == "subtitle_max_chars_per_line"
    assert entry.value == SUBTITLE_BASE_CHARS_PER_LINE - SUBTITLE_SHORTER_STEP_CHARS


def test_legacy_and_line_wrap_share_one_cumulative_sequence() -> None:
    """Given legacy subtitle_shorter then line_wrap, When derived, Then the
    widths step down cumulatively across both kinds in journal order."""

    given = (
        _applied("rcmd-bbbb00000001", "subtitle_shorter"),
        _applied("rcmd-bbbb00000002", "line_wrap"),
    )
    when = derive_presentation_overrides(given)
    assert [entry.value for entry in when.overrides] == [
        SUBTITLE_BASE_CHARS_PER_LINE - SUBTITLE_SHORTER_STEP_CHARS,
        SUBTITLE_BASE_CHARS_PER_LINE - 2 * SUBTITLE_SHORTER_STEP_CHARS,
    ]
    assert when.overrides[0].command_kind == "subtitle_shorter"
    assert when.overrides[1].command_kind == "line_wrap"


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("split_display", "字幕を分けて表示して"),
        ("duration_shorten", "字幕の表示時間を短くして"),
        ("text_summary_ack", "字幕を要約してよい"),
    ],
)
def test_no_knob_choice_apply_refused_with_honest_message(
    tmp_path: Path, kind: ReviewCommandKind, text: str
) -> None:
    """Given a confirmed no-knob choice (ack included), When applied,
    Then the honest typed refusal answers — never a journaled command."""

    assert SUBTITLE_NO_KNOB_REFUSAL_DETAIL == (
        "現在の仕組みではまだ対応していません。"
        "別の選択肢を選ぶか、相談へ戻ってください。"
    )
    store = ReviewStoreLocation(
        log_path=tmp_path / "events.jsonl", plan_dir=tmp_path / "store"
    )
    draft = interpret_command(text, ReviewChatContext(at_seconds=None))
    assert draft.command_kind == kind
    assert draft.needs_confirmation is False
    with pytest.raises(ReviewChatError) as exc_info:
        apply_command(draft, store=store)
    assert exc_info.value.code == "subtitle-choice-not-implemented"
    assert exc_info.value.detail == SUBTITLE_NO_KNOB_REFUSAL_DETAIL
    with pytest.raises(ReviewChatError) as validate_info:
        validate_draft_applicable(draft, store=store)
    assert validate_info.value.code == "subtitle-choice-not-implemented"


@pytest.mark.parametrize(
    "kind", ["split_display", "duration_shorten", "text_summary_ack"]
)
def test_no_knob_choice_apply_writes_nothing(
    tmp_path: Path, kind: ReviewCommandKind
) -> None:
    """Given a no-knob draft in a one-command bundle, When apply_drafts
    runs, Then it raises before any write: the sealed log is
    byte-identical, no applied-command journal exists (applied-command=0),
    and with no command nothing can schedule a rebuild (reservation=0)."""

    store = ReviewStoreLocation(
        log_path=tmp_path / "events.jsonl", plan_dir=tmp_path / "store"
    )
    initialize_store(manifest_plan("p0c-remove-clear"), store.log_path, store.plan_dir)
    events_before = store.log_path.read_bytes()
    draft = _draft(kind, f"no-knob {kind}", explicit_ack=True)

    with pytest.raises(ReviewChatError) as exc_info:
        apply_drafts([draft], episode_dir=tmp_path, store=store)

    assert exc_info.value.code == "subtitle-choice-not-implemented"
    assert store.log_path.read_bytes() == events_before
    assert load_head(store.log_path, store.plan_dir).version == 1
    assert not (tmp_path / "applied-commands.jsonl").exists()


def test_legacy_no_knob_journal_rows_still_derive_typed_notes() -> None:
    """Given HISTORICAL journaled no-knob entries (written before the
    apply refusal existed), When derived, Then the typed
    no-review-plane-knob notes still read back — read-compat only, no
    new row can be journaled through apply anymore."""

    given = (
        _applied("rcmd-cccc00000001", "split_display"),
        _applied("rcmd-cccc00000002", "duration_shorten"),
        _applied("rcmd-cccc00000003", "text_summary_ack", explicit_ack=True),
    )
    when = derive_presentation_overrides(given)
    assert when.overrides == ()
    assert [note.command_kind for note in when.notes] == [
        "split_display",
        "duration_shorten",
        "text_summary_ack",
    ]
    assert all(note.code == "no-review-plane-knob" for note in when.notes)
    assert when.to_render_settings() is None


def test_summary_without_ack_derivation_refused() -> None:
    """Given an unacked summary command, When derived, Then a typed
    summary-ack-required error is raised (never a fake effect)."""

    given = (_applied("rcmd-eeee00000001", "text_summary_ack"),)
    with pytest.raises(PresentationTranslationError, match="summary-ack-required"):
        derive_presentation_overrides(given)


def test_legacy_subtitle_shorter_history_stays_read_compatible() -> None:
    """Given a legacy subtitle_shorter journal entry, When derived, Then it
    still narrows the width (history fact preserved)."""

    given = tuple(_applied(f"rcmd-ffff0000000{i}", "subtitle_shorter") for i in range(5))
    when = derive_presentation_overrides(given)
    assert [entry.value for entry in when.overrides] == [16, 12, 8, 8, 8]
    assert when.effective_subtitle_max_chars() == SUBTITLE_MIN_CHARS_PER_LINE


def test_wrap_width_rule_lives_in_subtitle_policy() -> None:
    """Given the concept split, When the wrap rule is called, Then the
    cumulative sequence matches the legacy derivation exactly."""

    assert frozenset({"subtitle_shorter", "line_wrap"}) == SUBTITLE_NARROWING_KINDS
    assert subtitle_wrap_widths(5) == (16, 12, 8, 8, 8)
    assert subtitle_wrap_widths(0) == ()


def test_split_files_stay_within_pure_loc_cap() -> None:
    """Given the concept split, When pure LOC is counted, Then each file
    stays within the 250-line cap."""

    services = Path(__file__).resolve().parents[2] / "services"
    for rel in (
        "episode_cockpit/presentation_overrides.py",
        "compile/subtitle_policy.py",
    ):
        lines = (services / rel).read_text(encoding="utf-8").splitlines()
        pure = [
            line
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert len(pure) <= 250, f"{rel} has {len(pure)} pure LOC"
