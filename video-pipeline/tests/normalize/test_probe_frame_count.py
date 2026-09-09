"""Metadata-first frame count: derived counts are honest, decode fallback survives."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import pytest

from services.ingest.probe import ProbeExecutionError
from services.normalize import probe as probe_module
from services.normalize.errors import NormalizeVerificationError
from services.normalize.probe import (
    _derive_frame_count,
    _round_half_up,
    probe_media_facts,
    probe_media_json,
)
from services.normalize.runner import _probe_or_block

MEDIA = Path("probe-frame-count-fixture.mp4")


def _video_stream(
    *,
    nb_frames="8459",
    duration_ts="8467459",
    time_base="1/30000",
    r_frame_rate="30000/1001",
    avg_frame_rate="30000/1001",
):
    stream = {
        "codec_type": "video",
        "codec_name": "hevc",
        "width": 3840,
        "height": 2160,
        "pix_fmt": "yuv420p10le",
        "r_frame_rate": r_frame_rate,
        "avg_frame_rate": avg_frame_rate,
        "time_base": time_base,
        "duration_ts": duration_ts,
    }
    if nb_frames is not None:
        stream["nb_frames"] = nb_frames
    return stream


def _payload(video):
    return {
        "streams": [video],
        "format": {"duration": "282.248633", "size": "1643791731"},
    }


def _decode_payload(*, nb_read_frames="8459"):
    video = _video_stream()
    video["nb_read_frames"] = nb_read_frames
    return _payload(video)


@dataclass
class _Result:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass
class _FakeFfprobe:
    """Fail-loud ffprobe stand-in: full decode must be explicitly allowed."""

    metadata: dict | None = field(default_factory=lambda: _payload(_video_stream()))
    metadata_rc: int = 0
    head_rc: int = 0
    decode: dict | None = None
    decode_rc: int = 0
    decode_stderr: str = ""
    calls: list = field(default_factory=list)

    def __call__(self, argv, **kwargs):
        args = tuple(argv)
        self.calls.append(args)
        if "-read_intervals" in args:
            stderr = "" if self.head_rc == 0 else "head decode error"
            return _Result(self.head_rc, "{}", stderr)
        if "-count_frames" in args:
            if self.decode is None:
                raise AssertionError(f"full decode attempted: {args}")
            return _Result(self.decode_rc, json.dumps(self.decode), self.decode_stderr)
        stdout = json.dumps(self.metadata) if self.metadata and self.metadata_rc == 0 else ""
        return _Result(self.metadata_rc, stdout, "")

    def full_decodes(self):
        return [c for c in self.calls if "-count_frames" in c and "-read_intervals" not in c]


@pytest.fixture
def fake_ffprobe(monkeypatch):
    fake = _FakeFfprobe()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(probe_module.subprocess, "run", fake)
    return fake


def test_stream_metadata_count_skips_full_decode(fake_ffprobe):
    fake_ffprobe.decode = None  # fail loud on any full-decode attempt
    facts = probe_media_facts(Path("/fake/ffprobe"), MEDIA)
    assert facts.video.nb_read_frames == 8459
    assert facts.video.frame_count_source == "stream_metadata"
    assert fake_ffprobe.full_decodes() == []


def test_probe_json_carries_derived_count(fake_ffprobe):
    payload = probe_media_json(Path("/fake/ffprobe"), MEDIA)
    video = next(s for s in payload["streams"] if s["codec_type"] == "video")
    assert video["nb_read_frames"] == 8459


def test_duration_fps_derivation_when_nb_frames_absent(fake_ffprobe):
    fake_ffprobe.metadata = _payload(_video_stream(nb_frames=None))
    fake_ffprobe.decode = None  # fail loud on any full-decode attempt
    facts = probe_media_facts(Path("/fake/ffprobe"), MEDIA)
    assert facts.video.nb_read_frames == 8459
    assert facts.video.frame_count_source == "duration_fps_derived"
    assert fake_ffprobe.full_decodes() == []


def test_inconsistent_nb_frames_falls_back_to_decode(fake_ffprobe):
    fake_ffprobe.metadata = _payload(_video_stream(nb_frames="12"))
    fake_ffprobe.decode = _decode_payload()
    facts = probe_media_facts(Path("/fake/ffprobe"), MEDIA)
    assert facts.video.nb_read_frames == 8459
    assert facts.video.frame_count_source == "decoded"
    assert len(fake_ffprobe.full_decodes()) == 1


def test_metadata_failure_falls_back_to_decode(fake_ffprobe):
    fake_ffprobe.metadata_rc = 1
    fake_ffprobe.decode = _decode_payload()
    facts = probe_media_facts(Path("/fake/ffprobe"), MEDIA)
    assert facts.video.nb_read_frames == 8459
    assert facts.video.frame_count_source == "decoded"


def test_corrupt_stream_keeps_typed_undecodable_failure(fake_ffprobe):
    fake_ffprobe.metadata_rc = 1
    fake_ffprobe.decode = _decode_payload()
    fake_ffprobe.decode_rc = 1
    fake_ffprobe.decode_stderr = "Invalid data found when processing input"
    with pytest.raises(ProbeExecutionError, match="Invalid data"):
        probe_media_facts(Path("/fake/ffprobe"), MEDIA)
    with pytest.raises(NormalizeVerificationError) as exc_info:
        _probe_or_block(Path("/fake/ffprobe"), MEDIA, "input")
    assert exc_info.value.reason_code == "undecodable_output"
    assert "input is not decodable" in str(exc_info.value)


def test_head_sample_failure_blocks_as_undecodable(fake_ffprobe):
    fake_ffprobe.head_rc = 1
    with pytest.raises(ProbeExecutionError, match="not decodable"):
        probe_media_facts(Path("/fake/ffprobe"), MEDIA)


def test_derivation_matches_real_clip_tick_exact():
    video = _video_stream(nb_frames=None)
    count, source = _derive_frame_count(video, _payload(video))
    assert (count, source) == (8459, "duration_fps_derived")


def test_round_half_up_not_bankers():
    assert _round_half_up(Fraction(1, 2)) == 1
    assert _round_half_up(Fraction(3, 2)) == 2
    assert _round_half_up(Fraction(5, 2)) == 3
    assert _round_half_up(Fraction(8459, 1)) == 8459


def test_derivation_rounding_half_cases():
    base = {"time_base": "1/60000", "r_frame_rate": "30000/1001"}
    video = _video_stream(nb_frames=None, duration_ts="1001", **base)
    assert _derive_frame_count(video, _payload(video)) == (1, "duration_fps_derived")
    video = _video_stream(nb_frames=None, duration_ts="3003", **base)
    assert _derive_frame_count(video, _payload(video)) == (2, "duration_fps_derived")
    video = _video_stream(nb_frames=None, duration_ts="5005", **base)
    assert _derive_frame_count(video, _payload(video)) == (3, "duration_fps_derived")


def test_tolerance_boundary_accepts_nearby_rejects_wild():
    video = _video_stream(nb_frames="8467")  # estimate 8459, tolerance 8
    assert _derive_frame_count(video, _payload(video)) == (8467, "stream_metadata")
    video = _video_stream(nb_frames="8468")
    assert _derive_frame_count(video, _payload(video)) is None


def test_degenerate_metadata_falls_back_to_decode():
    assert _derive_frame_count(_video_stream(nb_frames="0"), _payload(_video_stream())) is None
    assert (
        _derive_frame_count(_video_stream(nb_frames="-3"), _payload(_video_stream())) is None
    )
    assert (
        _derive_frame_count(_video_stream(r_frame_rate="0/0"), _payload(_video_stream()))
        is None
    )
    no_ticks = _video_stream(nb_frames=None)
    del no_ticks["duration_ts"]
    payload = _payload(no_ticks)
    payload["format"] = {"duration": "282.248633"}
    assert _derive_frame_count(no_ticks, payload) is None
