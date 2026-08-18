"""Typed request/response contract for the read-only Media Query API.

Phase-1 frozen surface (PRD 10.3): seven allowlisted methods only;
Review/past-Plan retrieval is a Phase-5 concern, absent by
construction. Strict ints everywhere (no floats); half-open spans;
``source_id`` matches the strict identifier pattern, which rejects
relative/absolute paths, URLs, and whitespace. ``limit`` <=
FROZEN_MAX_PAGE_SIZE; no request may match or address more than
FROZEN_ROW_BUDGET rows.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Sha256, StrictModel

FROZEN_MAX_PAGE_SIZE: Final = 50
FROZEN_ROW_BUDGET: Final = 500

FROZEN_METHOD_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {"episode_summary", "search_transcripts", "silence_ranges", "quality_ranges",
     "contact_sheets", "candidate_frames", "range_statistics"})
LIFECYCLE_SURFACE: Final[frozenset[str]] = frozenset({"open", "close"})
FROZEN_PUBLIC_SURFACE: Final[frozenset[str]] = FROZEN_METHOD_ALLOWLIST | LIFECYCLE_SURFACE

SourceIdValue = Annotated[str, StringConstraints(
    min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$", strict=True)]
TextQueryValue = Annotated[str, StringConstraints(min_length=1, max_length=200, strict=True)]


class ApiBudgetExceeded(Exception):  # noqa: N818 (frozen contract name from the plan)
    """A request matched or addressed more rows than the frozen budget."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"budget-exceeded: {detail}")
        self.code = "budget-exceeded"
        self.detail = detail


class Pagination(StrictModel):
    limit: int = Field(ge=1, le=FROZEN_MAX_PAGE_SIZE, strict=True)
    offset: int = Field(ge=0, strict=True)


def _require_forward(start: int, end: int) -> None:
    if end < start:
        raise PydanticCustomError("span_inverted", "span end must be >= start (half-open)")


class MsSpan(StrictModel):
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_forward(self) -> Self:
        _require_forward(self.start_ms, self.end_ms)
        return self


class SampleSpan(StrictModel):
    start_sample: int = Field(ge=0, strict=True)
    end_sample: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_forward(self) -> Self:
        _require_forward(self.start_sample, self.end_sample)
        return self


class FrameSpan(StrictModel):
    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_forward(self) -> Self:
        _require_forward(self.start_frame, self.end_frame)
        return self


class EpisodeSummaryRequest(StrictModel):
    source_id: SourceIdValue


class ContactSheetsRequest(StrictModel):
    source_id: SourceIdValue


class SearchTranscriptsRequest(StrictModel):
    source_id: SourceIdValue
    text_query: TextQueryValue
    pagination: Pagination


class SilenceRangesRequest(StrictModel):
    source_id: SourceIdValue
    span: SampleSpan
    pagination: Pagination


class QualityRangesRequest(StrictModel):
    source_id: SourceIdValue
    span: FrameSpan
    pagination: Pagination


class CandidateFramesRequest(StrictModel):
    source_id: SourceIdValue
    span: FrameSpan


class RangeStatisticsRequest(StrictModel):
    source_id: SourceIdValue
    span: SampleSpan | FrameSpan


class _LineageRow(StrictModel):
    source_id: str
    analyzer_version: str
    artifact_sha: Sha256
    confidence: int | None = None


class EpisodeSummaryRow(_LineageRow):
    span: None = None
    sha256: Sha256
    source_kind: Literal["audio", "video"]
    sample_rate: int | None = None
    channels: int | None = None
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    rate_num: int | None = None
    rate_den: int | None = None
    frame_count: int | None = None


class TranscriptHitRow(_LineageRow):
    span: MsSpan
    segment_index: int = Field(ge=0, strict=True)
    text: str


class SilenceHitRow(_LineageRow):
    span: SampleSpan
    kind: str
    sample_rate: int = Field(gt=0, strict=True)


class QualityHitRow(_LineageRow):
    span: FrameSpan
    kind: str
    rule_id: str


class ContactSheetRow(_LineageRow):
    span: FrameSpan
    sheet_sha256: Sha256
    frame_indexes: tuple[int, ...]
    cols: int = Field(gt=0, strict=True)
    rows: int = Field(gt=0, strict=True)
    thumb_w: int = Field(gt=0, strict=True)
    thumb_h: int = Field(gt=0, strict=True)
    generator: str
    cadence_frames: int = Field(gt=0, strict=True)


class CandidateFrameRow(_LineageRow):
    span: FrameSpan
    frame_index: int = Field(ge=0, strict=True)
    sheet_sha256: Sha256


class RangeStatisticRow(_LineageRow):
    span: SampleSpan | FrameSpan
    metric: str
    value: int
    unit: Literal["sample", "frame", "count"]


class _BoundedResponse(StrictModel):
    total: int = Field(ge=0, strict=True)


class _PageResponse(_BoundedResponse):
    limit: int = Field(ge=1, strict=True)
    offset: int = Field(ge=0, strict=True)


class EpisodeSummaryResponse(_BoundedResponse):
    rows: tuple[EpisodeSummaryRow, ...]


class SearchTranscriptsResponse(_PageResponse):
    rows: tuple[TranscriptHitRow, ...]


class SilenceRangesResponse(_PageResponse):
    rows: tuple[SilenceHitRow, ...]


class QualityRangesResponse(_PageResponse):
    rows: tuple[QualityHitRow, ...]


class ContactSheetsResponse(_BoundedResponse):
    rows: tuple[ContactSheetRow, ...]


class CandidateFramesResponse(_BoundedResponse):
    rows: tuple[CandidateFrameRow, ...]


class RangeStatisticsResponse(_BoundedResponse):
    rows: tuple[RangeStatisticRow, ...]


__all__ = [
    "FROZEN_MAX_PAGE_SIZE", "FROZEN_METHOD_ALLOWLIST", "FROZEN_PUBLIC_SURFACE", "FROZEN_ROW_BUDGET",
    "LIFECYCLE_SURFACE", "ApiBudgetExceeded", "CandidateFrameRow", "CandidateFramesRequest",
    "CandidateFramesResponse", "ContactSheetRow", "ContactSheetsRequest", "ContactSheetsResponse",
    "EpisodeSummaryRequest", "EpisodeSummaryResponse", "FrameSpan", "MsSpan", "Pagination",
    "QualityHitRow", "QualityRangesRequest", "QualityRangesResponse", "RangeStatisticRow",
    "RangeStatisticsRequest", "RangeStatisticsResponse", "SampleSpan", "SearchTranscriptsRequest",
    "SearchTranscriptsResponse", "SilenceHitRow", "SilenceRangesRequest", "SilenceRangesResponse",
]
