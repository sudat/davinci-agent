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


class BuildReport0A(ArtifactEnvelope[Literal["build_report_0a"]]):
    items: tuple[BuildItemEvidence0A, ...]
    timeline_fingerprint: Sha256
    output_hash: Sha256
    warnings: tuple[BuildWarning0A, ...]
    failures: tuple[BuildFailure0A, ...]
