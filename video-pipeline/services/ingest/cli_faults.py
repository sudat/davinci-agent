"""Offline fault scenarios for ingest QA (``QA_FAULT_FIXTURE`` pattern).

Faults drive the real registration flow against crafted inputs or wrapped
probe seams: video-only containers are crafted with the pinned ffmpeg,
truncation/mutation operate on real materialized fixture copies, and the
synthetic non-monotonic sequence plus DOVI payload are injected at the
documented seams (muxers reject non-monotonic dts; the pinned encoder drops
PQ/DOVI signaling, so no real container can carry them).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

import services.ingest.ingest as ingest_module
from services.ingest.fixture_inputs import materialize_fixture
from services.ingest.ingest import recipe_pointer, register_one
from services.ingest.probe import PacketTimestamps

TRUNCATION_FRACTION = 2


class FaultSpecError(Exception):
    """The fault fixture is not a valid fault specification."""


class FaultSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    fault: Literal[
        "missing_audio_stream",
        "truncated_container",
        "mutate_during_probe",
        "non_monotonic_packets",
        "dolby_vision_rpu",
    ]


def load_fault_spec(path: Path) -> FaultSpec:
    try:
        return FaultSpec.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise FaultSpecError(f"invalid fault fixture {path}: {error}") from error


@dataclass(frozen=True, slots=True)
class FaultRun:
    spec: FaultSpec
    manifest_path: Path
    ffmpeg: Path
    ffprobe: Path
    out: Path
    media_dir: Path
    pin: Path

    def _fixture_media(self) -> Path:
        return materialize_fixture(self.manifest_path, self.ffmpeg, self.media_dir)

    def _register(self, media: Path, recipe_id: str) -> int:
        manifest = register_one(
            original=media,
            ffprobe=self.ffprobe,
            recipe=recipe_pointer(self.pin, recipe_id),
            out=self.out,
        )
        codes = ",".join(reason.code for reason in manifest.eligibility.reasons)
        if manifest.eligibility.verdict == "supported":
            print("register: supported")
            return 0
        print(f"verdict=blocked reason={codes}")
        print(f"manifest={self.out}")
        return 2

    def run(self) -> int:
        handler = {
            "missing_audio_stream": self._missing_audio_stream,
            "truncated_container": self._truncated_container,
            "mutate_during_probe": self._mutate_during_probe,
            "non_monotonic_packets": self._non_monotonic_packets,
            "dolby_vision_rpu": self._dolby_vision_rpu,
        }[self.spec.fault]
        return handler()

    def _missing_audio_stream(self) -> int:
        media = self.media_dir / "fault-video-only.mov"
        subprocess.run(
            [
                str(self.ffmpeg), "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=320x180:rate=24", "-frames:v", "48", "-vf",
                "scale=320:180,setpts=N/(24*TB)", "-r", "24", "-c:v",
                "h264_videotoolbox", "-video_track_timescale", "24000", "-an", "-y",
                str(media),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return self._register(media, "p0b-cfr24")

    def _truncated_container(self) -> int:
        source = self._fixture_media()
        truncated = self.media_dir / "fault-truncated.mov"
        payload = source.read_bytes()
        truncated.write_bytes(payload[: len(payload) // TRUNCATION_FRACTION])
        return self._register(truncated, "p0b-cfr24")

    def _mutate_during_probe(self) -> int:
        source = self._fixture_media()
        media = self.media_dir / "fault-drifting.mov"
        shutil.copyfile(source, media)
        real_sampler = ingest_module.sample_packets

        def mutating_sampler(
            ffprobe: Path, media_path: Path, stream_index: int, count: int = 200
        ) -> tuple[PacketTimestamps, ...]:
            samples = real_sampler(ffprobe, media_path, stream_index, count)
            with media_path.open("ab") as stream:
                stream.write(b"X")
            return samples

        ingest_module.sample_packets = mutating_sampler
        try:
            return self._register(media, "p0b-cfr24")
        finally:
            ingest_module.sample_packets = real_sampler

    def _non_monotonic_packets(self) -> int:
        media = self._fixture_media()
        real_sampler = ingest_module.sample_packets

        def non_monotonic_sampler(
            ffprobe: Path, media_path: Path, stream_index: int, count: int = 200
        ) -> tuple[PacketTimestamps, ...]:
            if stream_index != 0:
                return real_sampler(ffprobe, media_path, stream_index, count)
            ticks = [index * 1000 for index in range(7)]
            ticks[3] = 500
            return tuple(PacketTimestamps(pts=tick, dts=tick) for tick in ticks)

        ingest_module.sample_packets = non_monotonic_sampler
        try:
            return self._register(media, "p0b-cfr24")
        finally:
            ingest_module.sample_packets = real_sampler

    def _dolby_vision_rpu(self) -> int:
        media = self._fixture_media()
        real_probe = ingest_module.probe_media

        def dovi_probe(ffprobe: Path, media_path: Path) -> dict[str, object]:
            payload = real_probe(ffprobe, media_path)
            streams = payload.get("streams")
            if isinstance(streams, list):
                for stream in streams:
                    if isinstance(stream, dict) and stream.get("codec_type") == "video":
                        stream["side_data_list"] = [
                            {"side_data_type": "DOVI configuration record", "dv_profile": 8}
                        ]
                        stream["color_transfer"] = "smpte2084"
            return payload

        ingest_module.probe_media = dovi_probe
        try:
            return self._register(media, "p0b-cfr24")
        finally:
            ingest_module.probe_media = real_probe
