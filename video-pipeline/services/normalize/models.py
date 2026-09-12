"""NormalizeRecord artifact (PRD 7/8 envelope): strict, float-free, immutable.

The record is the committed provenance unit for one Edit Mezzanine: exact argv
with absolute binary paths, locked tool identity, source/output identities,
target profile, drop/duplicate accounting from the frozen conform model, and
the honest replay-determinism policy (h264_videotoolbox container bytes are
not asserted stable; ffprobe fields, frame accounting, and the decoded-frame
hash define semantic equivalence — the Phase-0A/Todo-14 precedent).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    ArtifactRef,
    Identifier,
    Producer,
    RationalFrameRate,
    Sha256,
    StrictModel,
)
from services.foundation_io import canonical_model_bytes, sha256_file

PATH_ANNOTATION = Annotated[str, StringConstraints(min_length=1)]
FrameIndex = Annotated[int, Field(ge=0, strict=True)]

VerificationLevel = Literal["standard", "full_decode"]
"""Normalize verification depth, recorded honestly on the record.

- ``"standard"``: encoded file sha256 + ffprobe duration/frame-count/codec
  fields + the bounded head-packet/frame sanity probe (no full decode).
- ``"full_decode"``: additionally the sha256 over fully decoded raw video
  frames (``OutputSemantics.decoded_video_sha256`` is present).
"""


class FileIdentity(StrictModel):
    path: PATH_ANNOTATION
    sha256: Sha256
    size_bytes: int = Field(ge=0)


class ToolIdentity(StrictModel):
    ffmpeg_sha256: Sha256
    ffprobe_sha256: Sha256
    lock_sha256: Sha256


class TargetProfile(StrictModel):
    frame_rate: RationalFrameRate
    sample_rate: int = Field(gt=0)
    video_codec: str
    audio_codec: str
    container: str
    video_track_timescale: int = Field(gt=0)


class NormalizationPolicy(StrictModel):
    rotation: Literal["noautorotate-rotation-metadata-preserved-v1"]
    color: Literal["preserve-or-explicit-v1"]


class DropDupExpectation(StrictModel):
    output_frames: int = Field(ge=0)
    dropped: tuple[FrameIndex, ...]
    duplicated: tuple[FrameIndex, ...]


class DropDupAccounting(StrictModel):
    expected: DropDupExpectation
    basis: Literal["conform-frame-conversion-accounting-v1"]


class OutputSemantics(StrictModel):
    decoded_video_sha256: Sha256 | None = None
    observed_output_frames: int = Field(ge=0)


class ReplayPolicy(StrictModel):
    determinism: Literal["semantic-equivalence-h264-videotoolbox"]
    note: str


class NormalizeRecord(StrictModel):
    schema_version: Literal["normalize-record-v1"]
    artifact_type: Literal["normalize-record"]
    artifact_id: Identifier
    content_hash: Sha256
    producer: Producer
    inputs: tuple[ArtifactRef, ...]
    source: FileIdentity
    output: FileIdentity
    argv: tuple[str, ...]
    tool: ToolIdentity
    target: TargetProfile
    policy: NormalizationPolicy
    declared_video_pix_fmt: str | None = None
    declared_scale_height: int | None = None
    declared_conversions: tuple[str, ...] = ()
    drop_dup: DropDupAccounting
    output_semantics: OutputSemantics
    verification: VerificationLevel = "full_decode"
    replay: ReplayPolicy

    @model_validator(mode="after")
    def reject_output_source_hash_confusion(self) -> NormalizeRecord:
        if (
            self.source.path != self.output.path
            and self.source.sha256 == self.output.sha256
        ):
            raise PydanticCustomError(
                "hash_confusion",
                "output hash equals the source hash; a re-encoded mezzanine "
                "cannot be byte-identical to its original",
            )
        return self

    @model_validator(mode="after")
    def require_honest_verification_level(self) -> NormalizeRecord:
        decoded = self.output_semantics.decoded_video_sha256
        if self.verification == "standard" and decoded is not None:
            raise PydanticCustomError(
                "verification_mismatch",
                "verification 'standard' must not carry a decoded-video hash",
            )
        if self.verification == "full_decode" and decoded is None:
            raise PydanticCustomError(
                "verification_mismatch",
                "verification 'full_decode' requires the decoded-video hash",
            )
        return self


CONTENT_HASH_PLACEHOLDER = "0" * 64


def seal_normalize_record(record: NormalizeRecord) -> NormalizeRecord:
    placeholder = record.model_copy(update={"content_hash": CONTENT_HASH_PLACEHOLDER})
    digest = hashlib.sha256(canonical_model_bytes(placeholder)).hexdigest()
    return record.model_copy(update={"content_hash": digest})


def verify_normalize_record_hash(record: NormalizeRecord) -> bool:
    placeholder = record.model_copy(update={"content_hash": CONTENT_HASH_PLACEHOLDER})
    return hashlib.sha256(canonical_model_bytes(placeholder)).hexdigest() == (
        record.content_hash
    )


def verify_normalize_record(record: NormalizeRecord) -> bool:
    """Re-hash the committed files and the sealed content hash."""

    source = Path(record.source.path)
    output = Path(record.output.path)
    return (
        verify_normalize_record_hash(record)
        and source.is_file()
        and output.is_file()
        and sha256_file(source) == record.source.sha256
        and sha256_file(output) == record.output.sha256
        and output.stat().st_size == record.output.size_bytes
    )
