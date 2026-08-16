"""Source Manifest models (PRD 7/9.4): strict, float-free canonical fields.

Every temporal value is an integer tick count plus a rational time base, or a
num/den rational pair; float seconds never appear in canonical storage.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from services.contracts.primitives import (
    Identifier,
    PositiveInteger,
    Producer,
    RationalFrameRate,
    Sha256,
    StrictModel,
)

ReasonCode = Literal[
    "missing_stream",
    "corrupt_decode",
    "non_monotonic",
    "unsupported_hdr",
    "changed_original_detected",
    "missing_file",
]

PATH_ANNOTATION = Annotated[str, StringConstraints(min_length=1)]


class FileIdentity(StrictModel):
    path: str
    size_bytes: int = Field(ge=0)
    sha256: Sha256


class ContainerInfo(StrictModel):
    format_name: str
    format_long_name: str | None = None
    nb_streams: int = Field(ge=0)
    duration_num: int = Field(ge=0)
    duration_den: PositiveInteger


class HdrSignaling(StrictModel):
    dolby_vision_rpu: bool
    dolby_vision_profile: int | None = None
    hdr10_mastering_display: bool
    smpte2094: bool
    color_transfer: str | None = None


class VideoStreamRecord(StrictModel):
    index: int = Field(ge=0)
    codec_type: Literal["video"]
    codec_name: str
    time_base_num: PositiveInteger
    time_base_den: PositiveInteger
    start_pts: int = Field(ge=0)
    duration_num: int = Field(ge=0)
    duration_den: PositiveInteger
    r_frame_rate_num: PositiveInteger
    r_frame_rate_den: PositiveInteger
    avg_frame_rate_num: PositiveInteger
    avg_frame_rate_den: PositiveInteger
    width: PositiveInteger
    height: PositiveInteger
    pix_fmt: str
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    color_range: str | None = None
    nb_frames: int | None = None
    rotation_degrees: int | None = None
    hdr: HdrSignaling

    @property
    def frame_rate(self) -> RationalFrameRate:
        return RationalFrameRate(num=self.r_frame_rate_num, den=self.r_frame_rate_den)


class AudioStreamRecord(StrictModel):
    index: int = Field(ge=0)
    codec_type: Literal["audio"]
    codec_name: str
    time_base_num: PositiveInteger
    time_base_den: PositiveInteger
    start_pts: int = Field(ge=0)
    duration_num: int = Field(ge=0)
    duration_den: PositiveInteger
    sample_rate: PositiveInteger
    channels: PositiveInteger
    channel_layout: str | None = None
    start_offset_samples: int = Field(ge=0)


class StreamMonotonicity(StrictModel):
    stream_index: int = Field(ge=0)
    monotonic: bool
    first_violation_index: int | None
    sampled_packets: int = Field(ge=0)


class DeltaClass(StrictModel):
    delta_ticks: int = Field(ge=0)
    count: PositiveInteger


class VfrEvidence(StrictModel):
    is_vfr: bool
    delta_classes: tuple[DeltaClass, ...]
    time_base_num: PositiveInteger
    time_base_den: PositiveInteger
    sampled_packets: int = Field(ge=0)
    basis: Literal["packet-pts-deltas-v1"]


class RecipePointer(StrictModel):
    recipe_id: Identifier
    recipe_source: str
    args_sha256: Sha256
    note: Literal["placeholder: edit-source normalization executes in todo-22"] = (
        "placeholder: edit-source normalization executes in todo-22"
    )


class EligibilityReason(StrictModel):
    code: ReasonCode
    detail: str


class Eligibility(StrictModel):
    verdict: Literal["supported", "blocked"]
    reasons: tuple[EligibilityReason, ...] = ()


class ProbeRecord(StrictModel):
    ffprobe_path: str
    ffprobe_sha256: Sha256
    arguments: tuple[str, ...]


class SourceManifest(StrictModel):
    schema_version: Literal["source-manifest-v1"]
    artifact_type: Literal["source-manifest"]
    artifact_id: Identifier
    content_hash: Sha256
    producer: Producer
    file: FileIdentity
    container: ContainerInfo
    streams: tuple[VideoStreamRecord | AudioStreamRecord, ...]
    monotonicity: tuple[StreamMonotonicity, ...]
    vfr_evidence: VfrEvidence | None
    edit_source_recipe: RecipePointer
    eligibility: Eligibility
    probe: ProbeRecord
