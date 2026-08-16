from __future__ import annotations

from fractions import Fraction
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        strict=True,
    ),
]
ArtifactId = Identifier
SchemaVersion = Identifier
ItemId = Identifier
SourceId = Identifier
AvLinkId = Identifier
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", strict=True)]
Frame = Annotated[int, Field(ge=0, strict=True)]
TrackIndex = Annotated[int, Field(gt=0, strict=True)]
PositiveInteger = Annotated[int, Field(gt=0, strict=True)]
TrackKind = Literal["video", "audio"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RationalFrameRate(StrictModel):
    num: PositiveInteger
    den: PositiveInteger

    @property
    def as_fraction(self) -> Fraction:
        return Fraction(self.num, self.den)

    def duration_for(self, frame_count: Frame) -> Fraction:
        return Fraction(frame_count * self.den, self.num)


class SourceFrameSpan(StrictModel):
    start_frame: Frame
    end_frame: Frame
    rate: RationalFrameRate

    @model_validator(mode="after")
    def require_forward_span(self) -> SourceFrameSpan:
        if self.end_frame < self.start_frame:
            raise PydanticCustomError(
                "span_inverted",
                "end_frame must be greater than or equal to start_frame",
            )
        return self

    @property
    def length(self) -> int:
        return self.end_frame - self.start_frame


class RecordFrameSpan(StrictModel):
    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward_span(self) -> RecordFrameSpan:
        if self.end_frame < self.start_frame:
            raise PydanticCustomError(
                "span_inverted",
                "end_frame must be greater than or equal to start_frame",
            )
        return self

    @property
    def length(self) -> int:
        return self.end_frame - self.start_frame


class TrackRef(StrictModel):
    kind: TrackKind
    index: TrackIndex


class SourceRef(StrictModel):
    source_id: SourceId
    span: SourceFrameSpan


class Producer(StrictModel):
    name: Identifier
    version: Identifier


class ArtifactRef(StrictModel):
    artifact_id: ArtifactId
    sha256: Sha256


class ArtifactEnvelope[ArtifactKind: str](StrictModel):
    artifact_id: ArtifactId
    artifact_type: ArtifactKind
    schema_version: SchemaVersion
    content_hash: Sha256
    producer: Producer
    inputs: tuple[ArtifactRef, ...]
