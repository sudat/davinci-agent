"""Presentation-intent consumption: derivation, manifest, render settings.

Hermetic (no ffmpeg, no LLM): the deterministic translation layer
(intent → settings overrides) plus the render-level effects (SRT re-wrap,
trace record) that close the r9e ``presentation-intent-no-renderer-effect``
gap. Given/When/Then per behavior.
"""

from __future__ import annotations

from pathlib import Path

from services.compile.subtitle_policy import (
    SUBTITLE_BASE_CHARS_PER_LINE,
    SUBTITLE_MIN_CHARS_PER_LINE,
    SUBTITLE_SHORTER_STEP_CHARS,
)
from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.timeline_ir import TimelineItem0C
from services.episode_cockpit.presentation_overrides import (
    LOWER_BGM_STEP_MB,
    AppliedCommand,
    consume_presentation_intents,
    derive_presentation_overrides,
    load_journal_commands,
)
from services.episode_cockpit.review_chat import ReviewCommandKind, record_applied_command
from services.preview.srt import (
    expected_subtitle_cues,
    expected_subtitle_cues_wrapped,
    wrap_cue_text,
)

RATE = RationalFrameRate(num=30, den=1)
# 18 chars: fits the base width 20 on one line, splits at width 16
# (units "あいうえおかきくけこ、" + "さしすせそたち").
WRAP_PROBE_TEXT = "あいうえおかきくけこ、さしすせそたち"


def _applied(
    command_id: str, kind: ReviewCommandKind, domain: str = "presentation"
) -> AppliedCommand:
    return AppliedCommand(
        command_id=command_id,
        command_kind=kind,
        affected_domain=domain,  # type: ignore[arg-type]
    )


def _subtitle_item(text: str) -> TimelineItem0C:
    return TimelineItem0C(
        item_id="sub-001",
        kind="subtitle",
        source=SourceRef(
            source_id="edit-source",
            span=SourceFrameSpan(start_frame=0, end_frame=30, rate=RATE),
        ),
        record_span=RecordFrameSpan(start_frame=0, end_frame=30),
        subtitle_text=text,
    )


def test_subtitle_shorter_sets_width_override_and_rewraps_srt() -> None:
    """Given one applied subtitle_shorter, When derived, Then the compile
    settings carry the stepped width AND the rendered SRT text changes."""

    given = (_applied("rcmd-aaaa00000001", "subtitle_shorter"),)
    when = derive_presentation_overrides(given)
    assert len(when.overrides) == 1
    entry = when.overrides[0]
    assert entry.command_id == "rcmd-aaaa00000001"
    assert entry.setting == "subtitle_max_chars_per_line"
    assert entry.value == SUBTITLE_BASE_CHARS_PER_LINE - SUBTITLE_SHORTER_STEP_CHARS
    settings = when.to_render_settings()
    assert settings is not None
    assert settings.subtitle_max_chars_per_line == entry.value
    assert settings.bgm_gain_mb is None
    items = (_subtitle_item(WRAP_PROBE_TEXT),)
    assert wrap_cue_text(WRAP_PROBE_TEXT, SUBTITLE_BASE_CHARS_PER_LINE) == WRAP_PROBE_TEXT
    shortened = wrap_cue_text(WRAP_PROBE_TEXT, entry.value)
    assert shortened != WRAP_PROBE_TEXT
    assert "\n" in shortened
    plain = expected_subtitle_cues(items, RATE)
    wrapped = expected_subtitle_cues_wrapped(items, RATE, entry.value)
    assert wrapped[0].text != plain[0].text
    assert (wrapped[0].start_ms, wrapped[0].end_ms) == (
        plain[0].start_ms,
        plain[0].end_ms,
    )


def test_no_intents_leaves_settings_untouched() -> None:
    """Given an empty journal, When derived/consumed, Then settings are
    None and no compile-manifest file is written (plain rebuilds keep
    byte-identical compile inputs)."""

    when = derive_presentation_overrides(())
    assert when.is_empty()
    assert when.to_render_settings() is None
    assert when.to_trace_presentation() is None


def test_consume_with_empty_journal_writes_no_manifest(tmp_path: Path) -> None:
    """Given an episode dir with no applied journal, When consumed, Then
    the plan dir gains no manifest file."""

    plan_dir = tmp_path / "review" / "store"
    plan_dir.mkdir(parents=True)
    when = consume_presentation_intents(
        tmp_path, plan_dir, head_version=4, plan_version="v4"
    )
    assert when.is_empty()
    assert list(plan_dir.iterdir()) == []


def test_edit_plan_domain_commands_are_unaffected() -> None:
    """Given span-translatable edit_plan commands, When derived, Then no
    override and no note is produced (bounded to presentation kinds)."""

    given = (
        _applied("rcmd-bbbb00000001", "remove_section", "edit_plan"),
        _applied("rcmd-bbbb00000002", "keep_longer", "edit_plan"),
        _applied("rcmd-bbbb00000003", "quiet_longer", "edit_plan"),
        _applied("rcmd-bbbb00000004", "use_other_take", "selection"),
        _applied("rcmd-bbbb00000005", "episode_only", "scope"),
    )
    when = derive_presentation_overrides(given)
    assert when.is_empty()
    assert when.to_render_settings() is None


def test_unimplemented_kind_records_typed_note_without_effect() -> None:
    """Given match_color (no review-plane knob), When derived, Then a
    typed no-review-plane-knob note is recorded and no setting changes."""

    given = (_applied("rcmd-cccc00000001", "match_color"),)
    when = derive_presentation_overrides(given)
    assert when.overrides == ()
    assert len(when.notes) == 1
    note = when.notes[0]
    assert note.command_id == "rcmd-cccc00000001"
    assert note.command_kind == "match_color"
    assert note.code == "no-review-plane-knob"
    assert len(note.detail) > 0
    assert when.to_render_settings() is None
    trace = when.to_trace_presentation()
    assert trace is not None
    assert trace.subtitle_max_chars_per_line is None
    assert trace.bgm_gain_mb is None
    assert len(trace.notes) == 1
    assert trace.notes[0].command_id == "rcmd-cccc00000001"


def test_two_intents_accumulate_in_journal_order() -> None:
    """Given subtitle_shorter then lower_bgm, When derived, Then both
    overrides are present in journal order with cumulative values."""

    given = (
        _applied("rcmd-dddd00000001", "subtitle_shorter"),
        _applied("rcmd-dddd00000002", "lower_bgm"),
    )
    when = derive_presentation_overrides(given)
    assert [entry.command_id for entry in when.overrides] == [
        "rcmd-dddd00000001",
        "rcmd-dddd00000002",
    ]
    assert when.overrides[0].setting == "subtitle_max_chars_per_line"
    assert when.overrides[1].setting == "bgm_gain_mb"
    assert when.overrides[1].value == LOWER_BGM_STEP_MB
    settings = when.to_render_settings()
    assert settings is not None
    assert (
        settings.subtitle_max_chars_per_line
        == SUBTITLE_BASE_CHARS_PER_LINE - SUBTITLE_SHORTER_STEP_CHARS
    )
    assert settings.bgm_gain_mb == LOWER_BGM_STEP_MB
    trace = when.to_trace_presentation()
    assert trace is not None
    assert trace.applied_command_ids == ("rcmd-dddd00000001", "rcmd-dddd00000002")


def test_repeated_shorter_steps_down_to_the_floor() -> None:
    """Given five subtitle_shorter intents, When derived, Then the width
    steps down cumulatively and clamps at the minimum."""

    given = tuple(_applied(f"rcmd-eeee0000000{i}", "subtitle_shorter") for i in range(5))
    when = derive_presentation_overrides(given)
    values = [entry.value for entry in when.overrides]
    assert values == [16, 12, 8, 8, 8]
    assert values[-1] == SUBTITLE_MIN_CHARS_PER_LINE
    assert when.effective_subtitle_max_chars() == SUBTITLE_MIN_CHARS_PER_LINE


def test_consume_round_trips_journal_order_and_manifest(tmp_path: Path) -> None:
    """Given a two-command journal on disk, When consumed, Then journal
    order is preserved and the compile manifest records both overrides
    with their command ids."""

    record_applied_command(tmp_path, _applied("rcmd-ffff00000001", "subtitle_shorter"))
    record_applied_command(tmp_path, _applied("rcmd-ffff00000002", "lower_bgm"))
    assert [command.command_id for command in load_journal_commands(tmp_path)] == [
        "rcmd-ffff00000001",
        "rcmd-ffff00000002",
    ]
    plan_dir = tmp_path / "review" / "store"
    plan_dir.mkdir(parents=True)
    when = consume_presentation_intents(
        tmp_path, plan_dir, head_version=4, plan_version="v4"
    )
    manifest_path = plan_dir / "presentation-overrides-v4.json"
    assert manifest_path.is_file()
    payload = manifest_path.read_bytes().decode()
    assert "rcmd-ffff00000001" in payload
    assert "rcmd-ffff00000002" in payload
    assert "subtitle_max_chars_per_line" in payload
    assert "bgm_gain_mb" in payload
    assert [entry.command_id for entry in when.overrides] == [
        "rcmd-ffff00000001",
        "rcmd-ffff00000002",
    ]
