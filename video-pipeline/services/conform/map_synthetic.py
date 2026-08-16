"""Synthetic internally-consistent SourceManifest/NormalizeRecord/MapFacts worlds.

Used by the offline fault scenarios (and unit tests): the drop/dup accounting
is computed through the frozen conform model, never hand-typed, so crafted
corruptions exercise the real builder and validator without media.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from services.conform.convert import frame_conversion_accounting
from services.conform.map_models import (
    EditAudioFacts,
    MapFacts,
    OriginalAudioFacts,
    OriginalVideoFacts,
)
from services.contracts.primitives import (
    ArtifactRef,
    Producer,
    RationalFrameRate,
    SourceFrameSpan,
)
from services.ingest.models import (
    AudioStreamRecord,
    ContainerInfo,
    Eligibility,
    HdrSignaling,
    ProbeRecord,
    RecipePointer,
    SourceManifest,
    VideoStreamRecord,
)
from services.ingest.models import (
    FileIdentity as IngestFileIdentity,
)
from services.normalize.models import (
    DropDupAccounting,
    DropDupExpectation,
    NormalizationPolicy,
    NormalizeRecord,
    OutputSemantics,
    ReplayPolicy,
    TargetProfile,
    ToolIdentity,
)
from services.normalize.models import (
    FileIdentity as NormalizeFileIdentity,
)


@dataclass(frozen=True, slots=True)
class SyntheticWorld:
    manifest: SourceManifest
    record: NormalizeRecord
    facts: MapFacts


def _sha(character: str) -> str:
    return character * 64


RATE_30 = RationalFrameRate(num=30, den=1)
RATE_24 = RationalFrameRate(num=24, den=1)


def synthetic_world(
    *,
    rate: RationalFrameRate = RATE_30,
    target: RationalFrameRate = RATE_30,
    tb_den: int = 30000,
    suffix: str = "a",
    frames: int = 10,
) -> SyntheticWorld:
    duration_seconds = Fraction(frames * rate.den, rate.num)
    samples = duration_seconds * 48000
    if samples.denominator != 1:
        raise ValueError(f"synthetic audio span is not integral: {samples}")
    video = VideoStreamRecord(
        index=0,
        codec_type="video",
        codec_name="synthetic",
        time_base_num=1,
        time_base_den=tb_den,
        start_pts=0,
        duration_num=duration_seconds.numerator,
        duration_den=duration_seconds.denominator,
        r_frame_rate_num=rate.num,
        r_frame_rate_den=rate.den,
        avg_frame_rate_num=rate.num,
        avg_frame_rate_den=rate.den,
        width=320,
        height=240,
        pix_fmt="yuv420p",
        nb_frames=frames,
        rotation_degrees=None,
        hdr=HdrSignaling(
            dolby_vision_rpu=False,
            dolby_vision_profile=None,
            hdr10_mastering_display=False,
            smpte2094=False,
            color_transfer=None,
        ),
    )
    audio = AudioStreamRecord(
        index=1,
        codec_type="audio",
        codec_name="pcm_s16le",
        time_base_num=1,
        time_base_den=48000,
        start_pts=0,
        duration_num=samples.numerator,
        duration_den=samples.denominator,
        sample_rate=48000,
        channels=1,
        channel_layout=None,
        start_offset_samples=0,
    )
    manifest = SourceManifest(
        schema_version="source-manifest-v1",
        artifact_type="source-manifest",
        artifact_id=f"synthetic-source-{suffix}",
        content_hash=_sha("1"),
        producer=Producer(name="services.ingest", version="1"),
        file=IngestFileIdentity(
            path=f"/synthetic/{suffix}/original.mov",
            size_bytes=1000,
            sha256=_sha("2"),
        ),
        container=ContainerInfo(
            format_name="mov",
            format_long_name=None,
            nb_streams=2,
            duration_num=duration_seconds.numerator,
            duration_den=duration_seconds.denominator,
        ),
        streams=(video, audio),
        monotonicity=(),
        vfr_evidence=None,
        edit_source_recipe=RecipePointer(
            recipe_id=f"synthetic-{suffix}",
            recipe_source="/synthetic/pins.json",
            args_sha256=_sha("3"),
        ),
        eligibility=Eligibility(verdict="supported", reasons=()),
        probe=ProbeRecord(
            ffprobe_path="/synthetic/ffprobe",
            ffprobe_sha256=_sha("4"),
            arguments=("-show_streams",),
        ),
    )
    span = SourceFrameSpan(
        start_frame=0,
        end_frame=frames,
        rate=rate,
    )
    accounting = frame_conversion_accounting(
        span, target
    )
    record = NormalizeRecord(
        schema_version="normalize-record-v1",
        artifact_type="normalize-record",
        artifact_id=f"synthetic-record-{suffix}",
        content_hash=_sha("5"),
        producer=Producer(name="services.normalize", version="1"),
        inputs=(ArtifactRef(artifact_id=manifest.artifact_id, sha256=_sha("1")),),
        source=NormalizeFileIdentity(
            path=manifest.file.path, sha256=_sha("2"), size_bytes=1000
        ),
        output=NormalizeFileIdentity(
            path=f"/synthetic/{suffix}/edit-source.mov",
            sha256=_sha("6"),
            size_bytes=2000,
        ),
        argv=("/synthetic/ffmpeg",),
        tool=ToolIdentity(
            ffmpeg_sha256=_sha("7"),
            ffprobe_sha256=_sha("8"),
            lock_sha256=_sha("9"),
        ),
        target=TargetProfile(
            frame_rate=target,
            sample_rate=48000,
            video_codec="synthetic",
            audio_codec="pcm_s16le",
            container="mov",
            video_track_timescale=tb_den,
        ),
        policy=NormalizationPolicy(
            rotation="noautorotate-rotation-metadata-preserved-v1",
            color="preserve-or-explicit-v1",
        ),
        drop_dup=DropDupAccounting(
            expected=DropDupExpectation(
                output_frames=accounting.output_frames,
                dropped=accounting.dropped_source_frames,
                duplicated=accounting.duplicated_source_frames,
            ),
            basis="conform-frame-conversion-accounting-v1",
        ),
        output_semantics=OutputSemantics(
            decoded_video_sha256=_sha("a"),
            observed_output_frames=accounting.output_frames,
        ),
        replay=ReplayPolicy(
            determinism="semantic-equivalence-h264-videotoolbox",
            note="synthetic",
        ),
    )
    facts = MapFacts(
        video=OriginalVideoFacts(
            nb_read_frames=frames,
            duration_num=duration_seconds.numerator,
            duration_den=duration_seconds.denominator,
            time_base_num=1,
            time_base_den=tb_den,
            start_pts=0,
        ),
        original_audio=OriginalAudioFacts(
            sample_rate=48000, sample_count=samples.numerator, start_offset_samples=0
        ),
        edit_audio=EditAudioFacts(
            sample_rate=48000,
            sample_count=samples.numerator,
            start_offset_samples=0,
        ),
    )
    return SyntheticWorld(manifest=manifest, record=record, facts=facts)
