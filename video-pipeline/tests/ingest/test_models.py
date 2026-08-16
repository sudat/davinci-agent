from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.conform.coordinates import OriginalTimestamp, RationalTimeBase
from services.contracts.primitives import Producer
from services.ingest import models
from services.ingest.commit import seal_manifest, verify_manifest_hash
from services.ingest.models import (
    AudioStreamRecord,
    ContainerInfo,
    DeltaClass,
    Eligibility,
    EligibilityReason,
    FileIdentity,
    RecipePointer,
    SourceManifest,
    StreamMonotonicity,
    VfrEvidence,
    VideoStreamRecord,
)
from services.ingest.records import (
    build_audio_record,
    build_video_record,
    format_duration_rational,
)
from tests.ingest.payloads import AUDIO_STREAM, DOVI_VIDEO_STREAM, FORMAT_PAYLOAD

PIN_PATH = "config/toolchains/pins/normalize-recipes.json"


def _manifest() -> SourceManifest:
    video = VideoStreamRecord(
        index=0,
        codec_type="video",
        codec_name="h264",
        time_base_num=1,
        time_base_den=24000,
        start_pts=0,
        duration_num=25,
        duration_den=1,
        r_frame_rate_num=24,
        r_frame_rate_den=1,
        avg_frame_rate_num=24,
        avg_frame_rate_den=1,
        width=1920,
        height=1080,
        pix_fmt="yuv420p",
        nb_frames=600,
        rotation_degrees=None,
        hdr=models.HdrSignaling(
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
        duration_num=25,
        duration_den=1,
        sample_rate=48000,
        channels=1,
        channel_layout="mono",
        start_offset_samples=0,
    )
    return SourceManifest(
        schema_version="source-manifest-v1",
        artifact_type="source-manifest",
        artifact_id="source-manifest-0000000000000000",
        producer=Producer(name="services.ingest", version="1"),
        content_hash="0" * 64,
        file=FileIdentity(path="/synthetic/original.mov", size_bytes=1, sha256="a" * 64),
        container=ContainerInfo(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            format_long_name="QuickTime / MOV",
            nb_streams=2,
            duration_num=25,
            duration_den=1,
        ),
        streams=(video, audio),
        monotonicity=(
            StreamMonotonicity(
                stream_index=0, monotonic=True, first_violation_index=None, sampled_packets=200
            ),
        ),
        vfr_evidence=VfrEvidence(
            is_vfr=False,
            delta_classes=(DeltaClass(delta_ticks=1000, count=199),),
            time_base_num=1,
            time_base_den=24000,
            sampled_packets=200,
            basis="packet-pts-deltas-v1",
        ),
        edit_source_recipe=RecipePointer(
            recipe_id="p0b-cfr24",
            recipe_source=PIN_PATH,
            args_sha256="b" * 64,
        ),
        eligibility=Eligibility(verdict="supported", reasons=()),
        probe=models.ProbeRecord(
            ffprobe_path="/synthetic/ffprobe",
            ffprobe_sha256="c" * 64,
            arguments=("-v", "error"),
        ),
    )


def test_manifest_rejects_unknown_fields() -> None:
    payload = json.loads(_manifest().model_dump_json())
    payload["surprise"] = 1
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SourceManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("size_bytes", 1.5),
        ("start_pts", 0.0),
        ("nb_frames", 24.0),
        ("sample_rate", 48000.0),
    ],
)
def test_canonical_fields_reject_floats(field: str, value: float) -> None:
    payload = json.loads(_manifest().model_dump_json())
    if field in {"start_pts", "nb_frames"}:
        payload["streams"][0][field] = value
    elif field == "sample_rate":
        payload["streams"][1][field] = value
    else:
        payload["file"][field] = value
    with pytest.raises(ValidationError):
        SourceManifest.model_validate(payload)


def test_manifest_dump_contains_no_floats_anywhere() -> None:
    stack: list[object] = [_manifest().model_dump(mode="json")]
    while stack:
        node = stack.pop()
        assert not isinstance(node, float), f"float leaked into canonical manifest: {node}"
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list | tuple):
            stack.extend(node)


def test_reason_codes_are_constrained() -> None:
    EligibilityReason(code="missing_stream", detail="d")
    with pytest.raises(ValidationError):
        EligibilityReason.model_validate({"code": "not_a_reason", "detail": "d"})


def test_content_hash_seal_roundtrip_and_drift(tmp_path: Path) -> None:
    manifest = _manifest()
    sealed = seal_manifest(manifest)
    assert sealed.content_hash != "0" * 64
    assert verify_manifest_hash(sealed) is True
    tampered = sealed.model_copy(
        update={"container": sealed.container.model_copy(update={"nb_streams": 3})}
    )
    assert verify_manifest_hash(tampered) is False
    assert tmp_path.is_dir()


def test_video_record_from_payload_parses_dovi_side_data() -> None:
    record = build_video_record(DOVI_VIDEO_STREAM)
    assert record.hdr.dolby_vision_rpu is True
    assert record.hdr.dolby_vision_profile == 8
    assert record.hdr.hdr10_mastering_display is True
    assert record.hdr.smpte2094 is True
    assert record.hdr.color_transfer == "smpte2084"
    assert record.duration_num == 10
    assert record.duration_den == 1


def test_audio_record_from_payload_computes_exact_offset() -> None:
    stream = dict(AUDIO_STREAM)
    stream["start_pts"] = 24000  # 0.5 s at 1/48000
    record = build_audio_record(stream)
    assert record.start_offset_samples == 24000
    assert record.duration_num == 1
    assert record.duration_den == 1


def test_format_duration_parses_decimal_exactly() -> None:
    rational = format_duration_rational(FORMAT_PAYLOAD)
    assert rational == (1, 1)
    assert Fraction(rational[0], rational[1]) == Fraction(1)


def test_start_timestamp_uses_integer_pts_and_time_base() -> None:
    stream = dict(DOVI_VIDEO_STREAM)
    record = build_video_record(stream)
    start = OriginalTimestamp(
        pts=record.start_pts,
        time_base=RationalTimeBase(num=record.time_base_num, den=record.time_base_den),
    )
    assert start.seconds == Fraction(0)
