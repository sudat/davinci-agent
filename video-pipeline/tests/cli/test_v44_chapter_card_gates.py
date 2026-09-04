"""Master-facts/probe/pinned-runner/audio-proof gates (TDD)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import services.cli._v44_chapter_card_probe as probe
from services.cli._v44_chapter_card_gates import (
    ChapterCardInsertError,
    require_master_facts,
)
from services.cli._v44_chapter_card_media import ChapterCardMediaError, run_pinned
from services.cli._v44_chapter_card_plan import ChapterCardPlanError, subtitle_item_count
from services.cli._v44_chapter_card_probe import MasterFacts
from services.cli._v44_chapter_card_render import (
    MasterGeometry,
    SpliceInputs,
    render_master_argv,
)
from services.preview.tools import PinnedTools
from tests.cli.v44_chapter_episode_workspace import fake_tools

GEOMETRY = MasterGeometry(
    width=320, height=180, fps=30, record_frame=20, card_frames=9,
    bitrate="4M", video_timescale=15360,
)


def _facts() -> MasterFacts:
    return MasterFacts(
        video_codec="h264",
        video_time_base="1/15360",
        video_frames=7837,
        avg_frame_rate="30/1",
        width=1920,
        height=1080,
        audio_codec="pcm_s16le",
        sample_rate=48000,
        channels=2,
        audio_samples=12539136,
    )


def test_require_master_facts_accepts_the_measured_master() -> None:
    # Given: facts exactly as probed from the approved master
    # When: the facts gate runs
    # Then: it passes (video codec and stream time base are enforced, measured)
    require_master_facts(_facts(), Path("master.mov"))


@pytest.mark.parametrize(
    "facts",
    [
        replace(_facts(), video_codec="hevc"),
        replace(_facts(), video_time_base="1/90000"),
        replace(_facts(), video_frames=7836),
        replace(_facts(), avg_frame_rate="25/1"),
        replace(_facts(), width=3840),
        replace(_facts(), audio_codec="aac"),
        replace(_facts(), sample_rate=44100),
        replace(_facts(), channels=1),
        replace(_facts(), audio_samples=12539135),
    ],
)
def test_require_master_facts_refuses_drift(facts: MasterFacts) -> None:
    # Given: one drifted fact
    # When: the facts gate runs
    # Then: typed refusal (hardcoded codec/timebase no longer trusted)
    with pytest.raises(ChapterCardInsertError, match="master-facts-mismatch"):
        require_master_facts(facts, Path("master.mov"))


def _probe_payload(**video: object) -> str:
    video_stream: dict[str, object] = {
        "codec_type": "video", "codec_name": "h264", "time_base": "1/15360",
        "avg_frame_rate": "30/1", "nb_read_frames": "7837", "width": 1920, "height": 1080,
    }
    video_stream.update(video)
    audio_stream: dict[str, object] = {
        "codec_type": "audio", "codec_name": "pcm_s16le", "time_base": "1/48000",
        "sample_rate": "48000", "channels": 2, "duration_ts": 12539136,
    }
    return json.dumps({"streams": [video_stream, audio_stream]})


def test_probe_master_facts_measures_codec_and_time_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a well-formed ffprobe payload with string-typed integers
    # When: the probe runs
    # Then: codec, stream time base, and counts come back as measured facts
    marker = tmp_path / "bin"
    marker.write_bytes(b"m")
    monkeypatch.setattr(
        probe, "run_pinned", lambda tools, argv, timeout: _probe_payload().encode()
    )
    facts = probe.probe_master_facts(fake_tools(marker, marker), tmp_path / "m.mov")
    assert facts.video_codec == "h264"
    assert facts.video_time_base == "1/15360"
    assert facts.video_timescale == 15360
    assert facts.video_frames == 7837
    assert facts.audio_samples == 12539136


@pytest.mark.parametrize(
    "payload",
    [
        b"not json at all",
        b"[]",
        b'{"streams": []}',
        b'{"streams": [{"codec_type": "video"}]}',
        (
            b'{"streams": [{"codec_type": "video", "codec_name": "h264"},'
            b' {"codec_type": "audio", "codec_name": "pcm_s16le"}]}'
        ),
    ],
)
def test_probe_master_facts_refuses_malformed_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    # Given: malformed or incomplete ffprobe output
    # When: the probe runs
    # Then: typed media refusal, never a raw KeyError/JSON error
    marker = tmp_path / "bin"
    marker.write_bytes(b"m")
    monkeypatch.setattr(probe, "run_pinned", lambda tools, argv, timeout: payload)
    with pytest.raises(ChapterCardMediaError):
        probe.probe_master_facts(fake_tools(marker, marker), tmp_path / "m.mov")


def test_run_pinned_verifies_pins_before_every_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: pinned binaries whose hash drifted from the lock
    # When: a pinned command runs
    # Then: typed refusal and the subprocess is never launched
    def _boom(argv: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        message = "subprocess launched despite pin drift"
        raise AssertionError(message)

    monkeypatch.setattr(subprocess, "run", _boom)
    marker = tmp_path / "bin"
    marker.write_bytes(b"m")
    drifted = PinnedTools(
        ffmpeg=marker, ffprobe=marker, ffmpeg_sha256="0" * 64, ffprobe_sha256="0" * 64
    )
    with pytest.raises(ChapterCardMediaError, match="toolchain-pin-drift"):
        run_pinned(drifted, (str(marker), "-version"), timeout=10)


def test_run_pinned_translates_launch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: valid pins but an unlaunchable binary path
    # When: a pinned command runs
    # Then: OSError becomes a typed media refusal
    marker = tmp_path / "bin"
    marker.write_bytes(b"m")

    def _boom(argv: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(ChapterCardMediaError, match="command-launch-failed"):
        run_pinned(fake_tools(marker, marker), (str(marker), "-version"), timeout=10)


def test_run_pinned_translates_failure_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a subprocess exiting non-zero, and one that times out
    # When: pinned commands run
    # Then: both become typed media refusals
    marker = tmp_path / "bin"
    marker.write_bytes(b"m")
    tools = fake_tools(marker, marker)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 3, b"", b"boom"),
    )
    with pytest.raises(ChapterCardMediaError, match="command-failed"):
        run_pinned(tools, (str(marker), "-version"), timeout=10)

    def _timeout(argv: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=str(argv), timeout=1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    with pytest.raises(ChapterCardMediaError, match="command-timeout"):
        run_pinned(tools, (str(marker), "-version"), timeout=1)


def test_render_master_argv_carries_geometry_and_timescale(tmp_path: Path) -> None:
    # Given: the splice geometry
    # When: building the mux argv
    # Then: scale, filter graph boundary, bitrate, and timescale all derive from it
    argv = render_master_argv(
        tmp_path / "ffmpeg",
        SpliceInputs(
            source=tmp_path / "src.mp4",
            card_raw=tmp_path / "card.rgb",
            pcm=tmp_path / "master.pcm",
            output=tmp_path / "out.mov",
        ),
        GEOMETRY,
    )
    text = " ".join(argv)
    assert "-s 320x180" in text
    assert "-b:v 4M" in text
    assert "-video_track_timescale 15360" in text
    assert "lt(n\\,20)" in text
    assert "gte(n\\,20)" in text
    assert argv[-1] == str(tmp_path / "out.mov")


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b'{"plan": {}}',
        b'{"plan": {"items": {}}}',
        b'{"plan": {"items": [{"item_id": "st1"}]}}',
        b"not json",
    ],
)
def test_subtitle_item_count_refuses_malformed_plan(
    tmp_path: Path, payload: bytes
) -> None:
    # Given: a review plan that is not the expected JSON shape
    # When: counting subtitle items
    # Then: typed plan refusal, never a raw KeyError/JSON error
    path = tmp_path / "plan-v3.json"
    path.write_bytes(payload)
    with pytest.raises(ChapterCardPlanError, match="plan-invalid"):
        subtitle_item_count(path)


def test_subtitle_item_count_reads_the_real_shape(tmp_path: Path) -> None:
    # Given: a plan of the real shape
    # When: the count is taken
    # Then: only subtitle items count
    path = tmp_path / "plan-v3.json"
    items = [{"kind": "video"}, {"kind": "subtitle"}, {"kind": "subtitle"}]
    path.write_text(json.dumps({"plan": {"items": items}}), encoding="utf-8")
    assert subtitle_item_count(path) == 2
