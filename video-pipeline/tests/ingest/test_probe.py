from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.ingest.probe import (
    ProbeExecutionError,
    ProbeToolDriftError,
    parse_int_rational,
    parse_rate_rational,
    probe_media,
    sample_packets,
    verify_pinned_ffprobe,
)

if TYPE_CHECKING:
    from services.toolchain.models import Phase0BToolchainLock


def test_parse_int_rational_accepts_slash_and_single_forms() -> None:
    assert parse_int_rational("30000/1001") == (30000, 1001)
    assert parse_int_rational("24") == (24, 1)
    with pytest.raises(ValueError, match="rational"):
        parse_int_rational("24.0")
    with pytest.raises(ValueError, match="rational"):
        parse_int_rational("0/0")


def test_parse_rate_rational_rejects_unknown_rates() -> None:
    assert parse_rate_rational("60000/1001", "avg_frame_rate") == (60000, 1001)
    with pytest.raises(ProbeExecutionError, match="avg_frame_rate"):
        parse_rate_rational("0/0", "avg_frame_rate")


def test_verify_pinned_ffprobe_rejects_hash_drift(tmp_path: Path) -> None:
    impostor = tmp_path / "ffprobe"
    impostor.write_bytes(b"#!/bin/sh\nexit 0\n")
    with pytest.raises(ProbeToolDriftError, match="hash drift"):
        verify_pinned_ffprobe(impostor)
    missing = tmp_path / "missing"
    with pytest.raises(ProbeToolDriftError):
        verify_pinned_ffprobe(missing)


def test_pinned_ffprobe_hash_matches_frozen_lock(
    pinned_ffprobe: Path, phase0b_lock: Phase0BToolchainLock
) -> None:
    verify_pinned_ffprobe(pinned_ffprobe)
    assert pinned_ffprobe == Path(phase0b_lock.ffmpeg.ffprobe.path)


def test_probe_media_returns_streams_and_format(
    pinned_ffprobe: Path, fixture_media: Mapping[str, Path]
) -> None:

    payload = probe_media(pinned_ffprobe, fixture_media["p0b-cfr24"])
    streams = payload.get("streams")
    format_section = payload.get("format")
    assert isinstance(streams, list)
    assert len(streams) == 2
    assert isinstance(format_section, dict)
    assert {str(stream.get("codec_type")) for stream in streams} == {"video", "audio"}


def test_probe_media_reports_truncated_container_as_corrupt(
    pinned_ffprobe: Path, fixture_media: Mapping[str, Path], tmp_path: Path
) -> None:

    media = fixture_media["p0b-cfr24"]
    truncated = tmp_path / "truncated.mov"
    truncated.write_bytes(media.read_bytes()[: media.stat().st_size // 2])
    with pytest.raises(ProbeExecutionError, match=r"moov|Invalid|corrupt"):
        probe_media(pinned_ffprobe, truncated)


def test_sample_packets_is_bounded_and_typed(
    pinned_ffprobe: Path, fixture_media: dict
) -> None:

    samples = sample_packets(pinned_ffprobe, fixture_media["p0b-cfr24"], stream_index=0, count=8)
    assert len(samples) == 8
    dts_values = [item.dts for item in samples if item.dts is not None]
    assert len(dts_values) == 8
    assert dts_values == sorted(dts_values)
    assert all(item.pts is not None for item in samples)


def test_sample_packets_audio_stream_is_bounded(
    pinned_ffprobe: Path, fixture_media: dict
) -> None:

    samples = sample_packets(
        pinned_ffprobe, fixture_media["p0b-cfr24"], stream_index=1, count=16
    )
    assert len(samples) == 16


def test_probe_payload_exposes_exact_integer_timestamp_fields(
    pinned_ffprobe: Path, fixture_media: dict[str, Path]
) -> None:

    payload = probe_media(pinned_ffprobe, fixture_media["p0b-cfr24"])
    streams = payload["streams"]
    assert isinstance(streams, list)
    video = next(
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "video"
    )
    assert isinstance(video["start_pts"], int)
    assert isinstance(video["duration_ts"], int)
    assert Fraction(*parse_int_rational(str(video["time_base"]))) == Fraction(1, 24000)
