from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from services.contracts.primitives import (
    ArtifactEnvelope,
    AvLinkId,
    Identifier,
    ItemId,
    RecordFrameSpan,
    Sha256,
    SourceRef,
    StrictModel,
    TrackKind,
    TrackRef,
)


class ItemPlacement0A(StrictModel):
    item_id: ItemId
    kind: TrackKind
    source: SourceRef
    record_span: RecordFrameSpan
    track: TrackRef
    av_link_id: AvLinkId | None
    media_path: str = Field(min_length=1)


class BuildItemEvidence0A(StrictModel):
    requested: ItemPlacement0A
    observed: ItemPlacement0A


class BuildWarning0A(StrictModel):
    severity: Literal["warning"] = "warning"
    code: Identifier
    detail: Annotated[str, Field(min_length=1, strict=True)]


class BuildFailure0A(StrictModel):
    severity: Literal["failure"] = "failure"
    code: Identifier
    detail: Annotated[str, Field(min_length=1, strict=True)]


class RenderJobLifecycle0A(StrictModel):
    """Render job lifecycle recorded without parsing localized status strings.

    Completion is detected only via ``CompletionPercentage == 100`` (Todo-17
    capability finding); the timestamps are ISO-8601 UTC wall-clock stamps and
    ``poll_count`` counts GetRenderJobStatus polls.
    """

    job_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    poll_count: int = Field(ge=1)
    completion_percentage: int = Field(ge=0, le=100)
    completion_source: Literal["CompletionPercentage"]
    note: str = Field(min_length=1)


class RenderStreamSummary0A(StrictModel):
    codec_type: str = Field(min_length=1)
    codec_name: str | None = None
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    r_frame_rate: str | None = None
    avg_frame_rate: str | None = None
    nb_frames: str | None = None
    sample_rate: str | None = None
    channels: int | None = None
    duration: str | None = None


class RenderProbeSummary0A(StrictModel):
    streams: tuple[RenderStreamSummary0A, ...]
    format_name: str | None = None
    format_duration: str | None = None


class DecodeEvidence0A(StrictModel):
    argv: tuple[str, ...] = Field(min_length=1)
    exit_code: int
    stderr_tail: str = ""


class RenderOutputEvidence0A(StrictModel):
    output_path: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    ffprobe: RenderProbeSummary0A
    decode: DecodeEvidence0A


class HostBindings0A(StrictModel):
    """Exact host/adapter/fixture identity this report was produced against."""

    host_report_path: str = Field(min_length=1)
    host_report_sha256: Sha256
    resolve_product: str = Field(min_length=1)
    resolve_version: str = Field(min_length=1)
    resolve_build: str = Field(min_length=1)
    adapter_module: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    manifest_sha256: Sha256
    ffmpeg_path: str = Field(min_length=1)
    ffmpeg_sha256: Sha256
    ffprobe_path: str = Field(min_length=1)
    ffprobe_sha256: Sha256


class BuildReport0A(ArtifactEnvelope[Literal["build_report_0a"]]):
    items: tuple[BuildItemEvidence0A, ...]
    timeline_fingerprint: Sha256
    output_hash: Sha256
    render_job: RenderJobLifecycle0A
    render_output: RenderOutputEvidence0A
    bindings: HostBindings0A
    warnings: tuple[BuildWarning0A, ...]
    failures: tuple[BuildFailure0A, ...]
