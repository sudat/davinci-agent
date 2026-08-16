"""ConformMap artifact models (PRD 9.3): strict, float-free, immutable.

The map records the versioned correspondence between one Original (PTS ticks
on its stream time base) and one CFR Edit Source (integer edit frames and
integer audio samples). The video table is explicit — one typed row per
output frame — because hash-deterministic JSON beats Parquet at this scale
(the six frozen 0B variants are <= 750 output frames; scaling policy: switch
to a chunked table artifact only when a real episode exceeds it, never
speculatively). Every temporal field is an integer or an integer pair; the
audio content offset (e.g. the +1024 sample shift of ``p0b-audio-offset1024``)
is an explicit input, never inferred from media.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from services.conform.coordinates import (  # noqa: TC001 - Pydantic resolves fields at runtime
    OriginalTimestamp,
)
from services.contracts.primitives import (
    ArtifactRef,
    Identifier,
    PositiveInteger,
    Producer,
    RationalFrameRate,
    Sha256,
    StrictModel,
)
from services.contracts.serialization import artifact_content_hash, canonical_json_bytes

PATH_ANNOTATION = Annotated[str, StringConstraints(min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]


@dataclass(frozen=True, slots=True)
class OriginalVideoFacts:
    nb_read_frames: int
    duration_num: int
    duration_den: int
    time_base_num: int
    time_base_den: int
    start_pts: int


@dataclass(frozen=True, slots=True)
class OriginalAudioFacts:
    sample_rate: int
    sample_count: int
    start_offset_samples: int


@dataclass(frozen=True, slots=True)
class EditAudioFacts:
    sample_rate: int
    sample_count: int
    start_offset_samples: int


@dataclass(frozen=True, slots=True)
class MapFacts:
    video: OriginalVideoFacts
    original_audio: OriginalAudioFacts
    edit_audio: EditAudioFacts


class OriginalIdentity(StrictModel):
    source_id: Identifier
    path: PATH_ANNOTATION
    sha256: Sha256


class EditSourceIdentity(StrictModel):
    path: PATH_ANNOTATION
    sha256: Sha256
    normalize_record_artifact_id: Identifier
    normalize_record_content_hash: Sha256


class MapNormalization(StrictModel):
    recipe_id: Identifier
    target_frame_rate: RationalFrameRate
    sample_rate: PositiveInteger
    rotation_policy: Literal["noautorotate-rotation-metadata-preserved-v1"]
    rotation_degrees: int | None = None
    audio_content_offset_samples: NonNegativeInt = 0
    output_frames: NonNegativeInt
    dropped_source_frames: tuple[NonNegativeInt, ...]
    duplicated_source_frames: tuple[NonNegativeInt, ...]
    basis: Literal["conform-frame-conversion-accounting-v1"]


class VideoMapRow(StrictModel):
    edit_frame: NonNegativeInt
    source_frame: NonNegativeInt
    original_pts: OriginalTimestamp
    duplicate: bool


class VideoTable(StrictModel):
    type: Literal["pts_to_frame_table"]
    source_frame_count: PositiveInteger
    source_rate: RationalFrameRate
    output_frames: NonNegativeInt
    rows: tuple[VideoMapRow, ...]


class AudioAffineMap(StrictModel):
    type: Literal["sample_affine"]
    original_sample_rate: PositiveInteger
    edit_sample_rate: PositiveInteger
    origin_original_sample: NonNegativeInt
    origin_edit_sample: NonNegativeInt
    rate_equal: bool
    original_sample_count: NonNegativeInt
    edit_sample_count: NonNegativeInt


class AudioSamplePair(StrictModel):
    original_sample: NonNegativeInt
    edit_sample: NonNegativeInt


class AudioTableMap(StrictModel):
    type: Literal["sample_pair_table"]
    original_sample_rate: PositiveInteger
    edit_sample_rate: PositiveInteger
    original_sample_count: NonNegativeInt
    edit_sample_count: NonNegativeInt
    pairs: tuple[AudioSamplePair, ...]


AudioMap = Annotated[AudioAffineMap | AudioTableMap, Field(discriminator="type")]


class EditFrameSpan(StrictModel):
    start_frame: NonNegativeInt
    end_frame: NonNegativeInt

    @model_validator(mode="after")
    def require_forward_span(self) -> EditFrameSpan:
        if self.end_frame < self.start_frame:
            raise PydanticCustomError(
                "span_inverted",
                "end_frame {end} before start_frame {start}",
                {"end": self.end_frame, "start": self.start_frame},
            )
        return self

    @property
    def length(self) -> int:
        return self.end_frame - self.start_frame


class ConformMap(StrictModel):
    schema_version: Literal["conform-map-v1"]
    artifact_type: Literal["conform-map"]
    artifact_id: Identifier
    content_hash: Sha256
    producer: Producer
    inputs: tuple[ArtifactRef, ...]
    map_version: Literal["1"]
    original: OriginalIdentity
    edit_source: EditSourceIdentity
    normalization: MapNormalization
    video_table: VideoTable
    audio_map: AudioMap
    table_sha256: Sha256
    replay: Literal["identical-bytes-for-identical-inputs"]


def compute_table_sha256(
    video_table: VideoTable, audio_map: AudioAffineMap | AudioTableMap
) -> str:
    digest = hashlib.sha256()
    digest.update(canonical_json_bytes(video_table))
    digest.update(canonical_json_bytes(audio_map))
    return digest.hexdigest()


def seal_conform_map(conform_map: ConformMap) -> ConformMap:
    return conform_map.model_copy(
        update={"content_hash": artifact_content_hash(conform_map)}
    )


def verify_conform_map_hash(conform_map: ConformMap) -> bool:
    return artifact_content_hash(conform_map) == conform_map.content_hash
