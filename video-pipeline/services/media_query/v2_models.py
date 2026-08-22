"""Typed request/response contracts for the v2 read-only media-query surface.

Discipline inherited from the frozen v1 ``api_models`` (which is never
mutated): bounded pages (``V2_MAX_PAGE_SIZE``), a per-request row budget
(``V2_ROW_BUDGET``), strict types, half-open spans, and lineage
(``artifact_sha``) on every row. v2 owns its own constants — v1's frozen
values are referenced read-only for span geometry only. The v2 surface
covers the ten PRD 7.4 editing queries over the canonical
``MediaIntelligenceArtifact`` index.
"""

from __future__ import annotations

from typing import Annotated, Final, Self

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Sha256, StrictModel

# TC001 suppression: pydantic resolves Shot at class-creation time; a
# TYPE_CHECKING placement would break the ShotDetailResponse schema at runtime.
from services.media_intelligence.models import Shot  # noqa: TC001

V2_MAX_PAGE_SIZE: Final = 50
V2_ROW_BUDGET: Final = 500

V2_METHOD_ALLOWLIST: Final[frozenset[str]] = frozenset({
    "shots", "shot_detail", "best_moments", "transcript_range", "semantic_shot_search",
    "similar_shots", "visible_text_candidates", "visual_quality_ranges",
    "audio_energy_ranges", "scene_summary"})
V2_PUBLIC_SURFACE: Final[frozenset[str]] = V2_METHOD_ALLOWLIST | frozenset({"open", "close"})

ShotIdValue = Annotated[str, StringConstraints(
    min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$", strict=True)]
SourceIdValue = Annotated[str, StringConstraints(
    min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$", strict=True)]
TextQueryValue = Annotated[str, StringConstraints(
    min_length=1, max_length=200, pattern=r"^.*\S.*$", strict=True)]


class ApiBudgetExceededV2(Exception):  # noqa: N818 (v1 ApiBudgetExceeded naming style)
    """A v2 request matched or addressed more rows than the v2 budget."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"budget-exceeded: {detail}")
        self.code = "budget-exceeded"
        self.detail = detail


class ApiShotNotFoundV2(Exception):  # noqa: N818
    """A v2 request addressed a shot_id absent from the index."""

    def __init__(self, shot_id: str) -> None:
        super().__init__(f"not-found: shot_id {shot_id!r} is not in the index")
        self.code = "not-found"
        self.shot_id = shot_id


class ApiIndexErrorV2(Exception):  # noqa: N818
    """The v2 index could not be opened or is not a v2 index."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class V2Pagination(StrictModel):
    limit: int = Field(ge=1, le=V2_MAX_PAGE_SIZE, strict=True)
    offset: int = Field(ge=0, strict=True)


class FrameSpan(StrictModel):
    """Half-open ``[start_frame, end_frame)`` on the Edit Source."""

    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_forward(self) -> Self:
        if self.end_frame < self.start_frame:
            raise PydanticCustomError("span_inverted", "span end must be >= start (half-open)")
        return self


class ShotsRequest(StrictModel):
    span: FrameSpan
    pagination: V2Pagination


class ShotDetailRequest(StrictModel):
    shot_id: ShotIdValue


class BestMomentsRequest(StrictModel):
    span: FrameSpan | None = None
    select_potentials: tuple[str, ...] | None = None
    pagination: V2Pagination


class TranscriptRangeRequest(StrictModel):
    source_id: SourceIdValue
    span: FrameSpan
    pagination: V2Pagination


class SemanticSearchRequest(StrictModel):
    text_query: TextQueryValue
    span: FrameSpan | None = None
    pagination: V2Pagination


class SimilarShotsRequest(StrictModel):
    shot_id: ShotIdValue


class VisibleTextCandidatesRequest(StrictModel):
    span: FrameSpan | None = None
    text_query: TextQueryValue | None = None
    pagination: V2Pagination


class VisualQualityRangesRequest(StrictModel):
    span: FrameSpan | None = None
    flags: tuple[str, ...] | None = None
    pagination: V2Pagination


class AudioEnergyRangesRequest(StrictModel):
    min_energy: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    max_energy: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    span: FrameSpan | None = None
    pagination: V2Pagination


class SceneSummaryRequest(StrictModel):
    pass


class _LineageRow(StrictModel):
    artifact_sha: Sha256


class ShotRow(_LineageRow):
    shot_id: str
    span: FrameSpan
    description: str
    shot_size: str
    camera_motion: str
    framing: str | None = None
    role: str
    select_potential: str
    pacing: str
    confidence_editorial: str
    confidence_visual: str


class BestMomentRow(_LineageRow):
    shot_id: str
    span: FrameSpan
    frame: int = Field(ge=0, strict=True)
    why: str
    role: str
    select_potential: str


class TranscriptRangeRow(_LineageRow):
    shot_id: str
    segment_id: str
    span: FrameSpan
    text: str


class SemanticHitRow(_LineageRow):
    shot_id: str
    span: FrameSpan
    score: int = Field(ge=0, strict=True)
    matched_fields: tuple[str, ...]
    description: str


class SimilarShotRow(_LineageRow):
    shot_id: str
    ref_shot_id: str
    score: float | None = None


class VisibleTextRow(_LineageRow):
    shot_id: str
    span: FrameSpan
    text: str
    frame: int | None = None


class QualityRangeV2Row(_LineageRow):
    shot_id: str
    span: FrameSpan
    flag: str
    severity: str | None = None


class AudioEnergyRow(_LineageRow):
    shot_id: str
    span: FrameSpan
    energy: float
    loudness_db: float | None = None
    ambient_type: str | None = None


class NameCount(StrictModel):
    name: str
    count: int = Field(ge=0, strict=True)


class SceneSummaryRow(StrictModel):
    episode_id: str
    source_count: int = Field(ge=0, strict=True)
    shot_count: int = Field(ge=0, strict=True)
    covered_frames: int = Field(ge=0, strict=True)
    role_counts: tuple[NameCount, ...]
    shot_size_counts: tuple[NameCount, ...]
    artifact_shas: tuple[Sha256, ...]


class _BoundedV2(StrictModel):
    total: int = Field(ge=0, strict=True)


class _PagedV2(_BoundedV2):
    limit: int = Field(ge=1, strict=True)
    offset: int = Field(ge=0, strict=True)


class ShotsResponse(_PagedV2):
    rows: tuple[ShotRow, ...]


class ShotDetailResponse(_BoundedV2):
    shot: Shot
    artifact_sha: Sha256


class BestMomentsResponse(_PagedV2):
    rows: tuple[BestMomentRow, ...]


class TranscriptRangeResponse(_PagedV2):
    rows: tuple[TranscriptRangeRow, ...]


class SemanticSearchResponse(_PagedV2):
    rows: tuple[SemanticHitRow, ...]


class SimilarShotsResponse(_BoundedV2):
    not_indexed: bool
    rows: tuple[SimilarShotRow, ...]


class VisibleTextCandidatesResponse(_PagedV2):
    rows: tuple[VisibleTextRow, ...]


class VisualQualityRangesResponse(_PagedV2):
    rows: tuple[QualityRangeV2Row, ...]


class AudioEnergyRangesResponse(_PagedV2):
    rows: tuple[AudioEnergyRow, ...]


class SceneSummaryResponse(_BoundedV2):
    rows: tuple[SceneSummaryRow, ...]


__all__ = [
    "V2_MAX_PAGE_SIZE",
    "V2_METHOD_ALLOWLIST",
    "V2_PUBLIC_SURFACE",
    "V2_ROW_BUDGET",
    "ApiBudgetExceededV2",
    "ApiIndexErrorV2",
    "ApiShotNotFoundV2",
    "AudioEnergyRangesRequest",
    "AudioEnergyRangesResponse",
    "AudioEnergyRow",
    "BestMomentRow",
    "BestMomentsRequest",
    "BestMomentsResponse",
    "FrameSpan",
    "NameCount",
    "QualityRangeV2Row",
    "SceneSummaryRequest",
    "SceneSummaryResponse",
    "SceneSummaryRow",
    "SemanticHitRow",
    "SemanticSearchRequest",
    "SemanticSearchResponse",
    "ShotDetailRequest",
    "ShotDetailResponse",
    "ShotRow",
    "ShotsRequest",
    "ShotsResponse",
    "SimilarShotRow",
    "SimilarShotsRequest",
    "SimilarShotsResponse",
    "TranscriptRangeRequest",
    "TranscriptRangeResponse",
    "TranscriptRangeRow",
    "V2Pagination",
    "VisibleTextCandidatesRequest",
    "VisibleTextCandidatesResponse",
    "VisibleTextRow",
    "VisualQualityRangesRequest",
    "VisualQualityRangesResponse",
]
