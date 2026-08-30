"""Trusted native-render metadata record (Task 7 rerun/resume identity).

One strict record per deterministic output name, written atomically by the
``render_native`` handler only after every media gate passes. A rerun trusts
an existing output ONLY when this record exists, names the exact
deterministic path, carries a real prior job id (the sentinel is
unrepresentable), and binds the file's own sha256 plus the measured stream
facts — a file without a valid record is stale/untrusted and never reused.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import StrictModel

_SENTINEL_JOB_ID = "reused-existing"


class NativeRenderMeta(StrictModel):
    """The persisted identity + measured facts of one completed render."""

    schema_version: Literal["native-render-meta-v1"]
    custom_name: str = Field(min_length=1, strict=True)
    job_id: str = Field(min_length=1, strict=True)
    output_path: str = Field(min_length=1, strict=True)
    output_sha256: str = Field(min_length=64, max_length=64, strict=True)
    duration_seconds: float = Field(gt=0, strict=True)
    video_codec: str = Field(min_length=1, strict=True)
    width: int = Field(ge=1, strict=True)
    height: int = Field(ge=1, strict=True)
    avg_frame_rate: str = Field(min_length=1, strict=True)
    audio_codec: str = Field(min_length=1, strict=True)
    audio_channels: int = Field(ge=1, strict=True)
    audio_sample_rate: int = Field(ge=1, strict=True)
    has_subtitle_stream: bool = Field(strict=True)

    @model_validator(mode="after")
    def forbid_sentinel_job_id(self) -> NativeRenderMeta:
        if self.job_id == _SENTINEL_JOB_ID:
            raise PydanticCustomError(
                "sentinel_job_id",
                "job_id must be the real prior Resolve job id, never a sentinel",
            )
        return self


__all__ = ["NativeRenderMeta"]
