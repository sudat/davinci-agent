"""Structured blocking/eligibility outcomes for contract-violating inputs."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from services.conform.coordinates import OriginalTimestamp, RationalTimeBase
from services.foundation_io import sha256_file
from services.ingest import ingest as ingest_module
from services.ingest.analysis import evaluate_eligibility, monotonicity_report
from services.ingest.ingest import recipe_pointer, register_one
from services.ingest.probe import PacketTimestamps, ProbeToolDriftError
from tests.ingest.conftest import PIN_PATH
from tests.ingest.payloads import (
    AUDIO_STREAM,
    DOVI_VIDEO_STREAM,
    FORMAT_PAYLOAD,
    SMPTE2084_VIDEO_STREAM,
)

# Real crafted inputs (deterministic, pinned ffmpeg): ------------------------


def _craft_video_only(ffmpeg: Path, destination: Path) -> None:
    subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-frames:v",
            "48",
            "-vf",
            "scale=320:180,setpts=N/(24*TB)",
            "-r",
            "24",
            "-c:v",
            "h264_videotoolbox",
            "-video_track_timescale",
            "24000",
            "-an",
            "-y",
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_missing_audio_stream_blocks_with_missing_stream(
    pinned_ffmpeg: Path, pinned_ffprobe: Path, fixture_media: Mapping[str, Path], tmp_path: Path
) -> None:
    media = tmp_path / "video-only.mov"
    _craft_video_only(pinned_ffmpeg, media)
    manifest = register_one(
        original=media,
        ffprobe=pinned_ffprobe,
        recipe=recipe_pointer(PIN_PATH, "p0b-cfr24"),
        out=tmp_path / "blocked.json",
    )
    assert manifest.eligibility.verdict == "blocked"
    assert [reason.code for reason in manifest.eligibility.reasons] == ["missing_stream"]
    assert (tmp_path / "blocked.json").is_file()


def test_truncated_container_blocks_with_corrupt_decode(
    pinned_ffprobe: Path, fixture_media: Mapping[str, Path], tmp_path: Path
) -> None:
    source = fixture_media["p0b-cfr24"]
    truncated = tmp_path / "truncated.mov"
    truncated.write_bytes(source.read_bytes()[: source.stat().st_size // 2])
    manifest = register_one(
        original=truncated,
        ffprobe=pinned_ffprobe,
        recipe=recipe_pointer(PIN_PATH, "p0b-cfr24"),
        out=tmp_path / "blocked.json",
    )
    assert manifest.eligibility.verdict == "blocked"
    assert [reason.code for reason in manifest.eligibility.reasons] == ["corrupt_decode"]
    assert manifest.streams == ()


def test_changed_original_blocks_register_with_hash_drift(
    pinned_ffprobe: Path,
    fixture_media: Mapping[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "drifting.mov"
    media.write_bytes(fixture_media["p0b-cfr24"].read_bytes())
    original_hash = sha256_file(media)
    real_sampler = ingest_module.sample_packets

    def mutating_sampler(
        ffprobe: Path, media_path: Path, stream_index: int, count: int = 200
    ) -> tuple[PacketTimestamps, ...]:
        samples = real_sampler(ffprobe, media_path, stream_index, count)
        with media_path.open("ab") as stream:
            stream.write(b"X")  # simulates the original changing mid-register
        return samples

    monkeypatch.setattr(ingest_module, "sample_packets", mutating_sampler)
    manifest = register_one(
        original=media,
        ffprobe=pinned_ffprobe,
        recipe=recipe_pointer(PIN_PATH, "p0b-cfr24"),
        out=tmp_path / "blocked.json",
    )
    assert manifest.eligibility.verdict == "blocked"
    assert [reason.code for reason in manifest.eligibility.reasons] == [
        "changed_original_detected"
    ]
    assert manifest.file.sha256 == original_hash
    assert sha256_file(media) != original_hash


def test_hash_drifted_ffprobe_refuses_to_run(tmp_path: Path) -> None:
    impostor = tmp_path / "ffprobe"
    impostor.write_bytes(b"not ffprobe")
    with pytest.raises(ProbeToolDriftError, match="hash drift"):
        register_one(
            original=tmp_path / "any.mov",
            ffprobe=impostor,
            recipe=recipe_pointer(PIN_PATH, "p0b-cfr24"),
            out=tmp_path / "never.json",
        )


# Synthetic sequences (model-level, documented): -----------------------------


def _sequence(*ticks: int) -> list[OriginalTimestamp]:
    time_base = RationalTimeBase(num=1, den=24000)
    return [OriginalTimestamp(pts=tick, time_base=time_base) for tick in ticks]


def test_non_monotonic_synthetic_pts_blocks_with_first_violation_index() -> None:
    report = monotonicity_report(stream_index=0, samples=_sequence(0, 1000, 2000, 1500, 2500))
    assert report.monotonic is False
    assert report.first_violation_index == 3
    assert report.sampled_packets == 5


def test_monotonic_synthetic_pts_passes() -> None:
    report = monotonicity_report(stream_index=0, samples=_sequence(0, 1000, 2000, 3000))
    assert report.monotonic is True
    assert report.first_violation_index is None


def test_non_monotonic_register_blocks_via_sampler(
    pinned_ffprobe: Path,
    fixture_media: Mapping[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media = tmp_path / "nonmono.mov"
    media.write_bytes(fixture_media["p0b-cfr24"].read_bytes())
    real_sampler = ingest_module.sample_packets

    def non_monotonic_sampler(
        ffprobe: Path, media_path: Path, stream_index: int, count: int = 200
    ) -> tuple[PacketTimestamps, ...]:
        if stream_index != 0:
            return real_sampler(ffprobe, media_path, stream_index, count)
        ticks = [index * 1000 for index in range(7)]
        ticks[3] = 500  # decreasing dts/pts at index 3
        return tuple(PacketTimestamps(pts=tick, dts=tick) for tick in ticks)

    monkeypatch.setattr(ingest_module, "sample_packets", non_monotonic_sampler)
    manifest = register_one(
        original=media,
        ffprobe=pinned_ffprobe,
        recipe=recipe_pointer(PIN_PATH, "p0b-cfr24"),
        out=tmp_path / "blocked.json",
    )
    assert manifest.eligibility.verdict == "blocked"
    assert [reason.code for reason in manifest.eligibility.reasons] == ["non_monotonic"]
    video_entry = next(e for e in manifest.monotonicity if e.stream_index == 0)
    assert video_entry.first_violation_index == 3


def test_unsupported_hdr_blocks_at_model_level_with_documented_reason() -> None:
    """Pinned h264_videotoolbox drops PQ/DOVI signaling, so no real container
    can be crafted with the frozen toolchain; the payload-level check is the
    honest equivalent (brief allows model-level with documented reason)."""

    for stream in (DOVI_VIDEO_STREAM, SMPTE2084_VIDEO_STREAM):
        eligibility = evaluate_eligibility(
            streams_payload={"streams": [stream, AUDIO_STREAM]},
            monotonic=(),
            extra_reasons=(),
        )
        assert eligibility.verdict == "blocked"
        assert [reason.code for reason in eligibility.reasons] == ["unsupported_hdr"]


def test_supported_sdr_payload_passes_model_level_eligibility() -> None:
    sdr = dict(SMPTE2084_VIDEO_STREAM)
    sdr.pop("color_transfer")
    payload = {"streams": [sdr, AUDIO_STREAM], **FORMAT_PAYLOAD}
    eligibility = evaluate_eligibility(
        streams_payload=payload, monotonic=(), extra_reasons=()
    )
    assert eligibility.verdict == "supported"
