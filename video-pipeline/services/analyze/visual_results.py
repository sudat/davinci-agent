"""Span-result models for the minimum visual checks (Todo 35).

Every result carries the sha256 of the decode binding it is traceable to,
the decode frame count it claims against, half-open frame spans, frozen
confidence permille, and full rule provenance. A span whose frames exceed
what the decode produced — or that carries no binding at all — is a
validation error by construction (untraceable-frame guard).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Sha256, StrictModel


class CheckProvenance(StrictModel):
    analyzer_version: str = Field(min_length=1, strict=True)
    rule_id: str = Field(min_length=1, strict=True)
    decode_binding_sha256: Sha256
    input_artifact_hashes: tuple[str, ...] = Field(min_length=1)


class FrameSpan(StrictModel):
    """Half-open [start_frame, end_frame) frame span."""

    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_forward(self) -> FrameSpan:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("span_inverted", "end_frame must exceed start_frame")
        return self


class _SpanResult(StrictModel):
    span: FrameSpan
    confidence: int = Field(ge=0, le=1000, strict=True)
    provenance: CheckProvenance
    decode_frame_count: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_traceable_frames(self) -> _SpanResult:
        if self.span.end_frame > self.decode_frame_count:
            raise PydanticCustomError(
                "untraceable_frame", "span exceeds the frames the decode produced"
            )
        if self.provenance.decode_binding_sha256 == "0" * 64:
            raise PydanticCustomError("untraceable_frame", "span has no decode-evidence binding")
        return self


class SceneChangeResult(_SpanResult):
    boundary_frame: int = Field(gt=0, strict=True)
    mean_diff_m: int = Field(ge=0, le=1000, strict=True)

    @model_validator(mode="after")
    def require_boundary_shape(self) -> SceneChangeResult:
        if self.boundary_frame != self.span.start_frame:
            raise PydanticCustomError("boundary_mismatch", "scene span starts at boundary")
        if self.span.end_frame != self.boundary_frame + 1:
            raise PydanticCustomError("boundary_mismatch", "scene span is the single new frame")
        if self.boundary_frame >= self.decode_frame_count:
            raise PydanticCustomError("untraceable_frame", "boundary beyond decoded frames")
        return self


class BlackSpanResult(_SpanResult):
    span_mean_luma_m: int = Field(ge=0, le=1000, strict=True)
    min_black_pixel_fraction_m: int = Field(ge=0, le=1000, strict=True)


class BlurSpanResult(_SpanResult):
    max_laplacian_mean_square: int = Field(ge=0, strict=True)


class ExposureSpanResult(_SpanResult):
    direction: Literal["over", "under"]
    span_mean_luma_m: int = Field(ge=0, le=1000, strict=True)
    max_clipped_high_fraction_m: int = Field(ge=0, le=1000, strict=True)


__all__ = [
    "BlackSpanResult",
    "BlurSpanResult",
    "CheckProvenance",
    "ExposureSpanResult",
    "FrameSpan",
    "SceneChangeResult",
]
