"""Subtitle checks over a real mov_text track plus pure cue evaluation."""

from __future__ import annotations

from pathlib import Path

from services.preview.srt import SubtitleCue
from services.qc.checks import check_subtitles
from services.qc.tools import load_qc_tools
from tests.qc.support import RenderSpec, base_render, clean_policy, preset

INPUTS = ("9" * 64,)


def rules(issues) -> set[str]:
    return {issue.rule_id for issue in issues}


def _srt(*cues: tuple[int, int, str]) -> bytes:
    def stamp(ms: int) -> str:
        hours, rem = divmod(ms, 3600_000)
        minutes, rem = divmod(rem, 60_000)
        seconds, millis = divmod(rem, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    return "\n".join(
        f"{index + 1}\n{stamp(start)} --> {stamp(end)}\n{text}\n"
        for index, (start, end, text) in enumerate(cues)
    ).encode()


def test_missing_track_blocks_only_when_required() -> None:
    policy = clean_policy(preset(RenderSpec()))
    assert check_subtitles(None, (), policy, INPUTS) == ()
    required = clean_policy(preset(RenderSpec()))
    required = required.model_copy(
        update={
            "subtitle": required.subtitle.model_copy(update={"track_required": True}),
            "policy_sha256": "0" * 64,
        }
    )
    required = required.model_copy(update={"policy_sha256": required.content_hash()})
    assert rules(check_subtitles(None, (), required, INPUTS)) == {"subtitle_track_missing"}


def test_clean_cues_pass() -> None:
    track = _srt((500, 1500, "hello"), (2000, 3500, "world"))
    policy = clean_policy(preset(RenderSpec()))
    assert check_subtitles(track, (), policy, INPUTS) == ()


def test_short_cue_blocks_min_duration() -> None:
    track = _srt((500, 700, "too short"), (2000, 3500, "ok cue"))
    policy = clean_policy(preset(RenderSpec()))
    assert "subtitle_min_duration" in rules(check_subtitles(track, (), policy, INPUTS))


def test_overlap_blocks() -> None:
    track = _srt((500, 1500, "first"), (1200, 2400, "second"))
    policy = clean_policy(preset(RenderSpec()))
    assert "subtitle_cue_overlap" in rules(check_subtitles(track, (), policy, INPUTS))


def test_overlong_lines_and_safe_area_block() -> None:
    long_line = "x" * 50
    track = _srt((500, 1500, long_line), (2000, 3500, "ok"))
    policy = clean_policy(preset(RenderSpec()))
    found = rules(check_subtitles(track, (), policy, INPUTS))
    assert "subtitle_max_chars" in found
    two_line = "y" * 45 + "\n" + "z" * 45
    track2 = _srt((500, 1500, two_line), (2000, 3500, "ok"))
    found2 = rules(check_subtitles(track2, (), policy, INPUTS))
    assert "subtitle_safe_area" in found2


def test_max_lines_blocks() -> None:
    three_lines = "a\nb\nc"
    track = _srt((500, 1500, three_lines), (2000, 3500, "ok"))
    policy = clean_policy(preset(RenderSpec()))
    assert "subtitle_max_lines" in rules(check_subtitles(track, (), policy, INPUTS))


def test_timing_drift_vs_committed_blocks() -> None:
    track = _srt((500, 1500, "hello"), (2000, 3500, "world"))
    committed = (SubtitleCue(start_ms=800, end_ms=1800, text="hello"),)
    policy = clean_policy(preset(RenderSpec()))
    assert "subtitle_timing_drift" in rules(
        check_subtitles(track, committed, policy, INPUTS)
    )


def test_text_drift_blocks() -> None:
    track = _srt((500, 1500, "edited text"), (2000, 3500, "world"))
    committed = (SubtitleCue(start_ms=500, end_ms=1500, text="original text"),)
    policy = clean_policy(preset(RenderSpec()))
    assert "subtitle_text_drift" in rules(
        check_subtitles(track, committed, policy, INPUTS)
    )


def test_real_mov_text_track_demuxes_and_passes(tmp_path: Path) -> None:
    tools = load_qc_tools()
    spec = RenderSpec(srt_lines=((500, 1500, "hello qc"), (2000, 3500, "final gate")))
    render = base_render(tools.ffmpeg, tmp_path, spec, name="subbed.mp4")
    demuxed = tools.demux_subtitle(render)
    assert b"hello qc" in demuxed
    policy = clean_policy(preset(RenderSpec(audio_channels=2)))
    assert check_subtitles(demuxed, (), policy, INPUTS) == ()
