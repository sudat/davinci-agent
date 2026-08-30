"""T6 video-understanding runtime DTOs, typed errors, and seam protocols.

Strict, rebuildable, deterministic value types for the orchestration seam —
never registered as artifacts. The protocols mirror the CONCRETE T4 adapters
(``GeminiVideoReviewProvider``, ``GlmVisualSpecialistProvider``,
``ClipExtractor``) structurally: no base-provider hierarchy, no router —
typing-only seams exactly like ``moment_review``'s provider protocols.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final, Protocol

from pydantic import BeforeValidator, Field

from services.contracts.primitives import Frame, Identifier, RationalFrameRate, StrictModel
from services.media_intelligence.budget import (
    AnalysisBudgetV1,  # noqa: TC001 (pydantic field)
    DeepReviewWindow,  # noqa: TC001 (pydantic field)
)
from services.media_intelligence.lead_map import LeadMapWindowPolicy
from services.media_intelligence.moment_review import (
    MomentDeepReviewV1,  # noqa: TC001 (pydantic field)
    ReviewStageLineage,  # noqa: TC001 (pydantic field)
    ReviewWindow,  # noqa: TC001 (pydantic field)
)

if TYPE_CHECKING:
    from services.editorial_v2.editorial_pins import EditorialPinV2
    from services.media_intelligence.moment_review import (
        AudioContext,
        AudioContextSource,
        TranscriptLookup,
    )
    from services.media_intelligence.video_clip_evidence import VideoClipEvidence
    from services.media_intelligence.video_clip_extraction import ClipEvidencePair
    from services.media_intelligence.video_review_exchange import WireOutcome
    from services.media_intelligence.video_review_wire import (
        GeminiClipReview,
        GlmClipObservation,
    )
    from services.media_intelligence.video_stage_wire import GeminiStageResult


#: The tool identity stamped on every T6 stage and fused review lineage.
VIDEO_UNDERSTANDING_TOOL: Final = "video-understanding-v1"


class VideoUnderstandingError(ValueError):
    """Typed video-understanding orchestration failure — never a fallback."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class StageRangeMismatchError(VideoUnderstandingError):
    """A provider reported an analyzed span different from the request."""

    def __init__(self, detail: str) -> None:
        super().__init__("stage-range-mismatch", detail)


class HardSpecialistTargetError(VideoUnderstandingError):
    """A hard (required) specialist target failed — block, never omit."""

    def __init__(self, detail: str) -> None:
        super().__init__("hard-specialist-target-failed", detail)


class GeminiStageCaller(Protocol):
    """``GeminiVideoReviewProvider`` shape: the T6 trace seam.

    ``review_with_trace`` returns the stage-specific typed payload plus the
    ACTUAL transport attempt count (2 after the adapter's one parser retry).
    """

    pin: EditorialPinV2

    def review_with_trace(
        self, purpose: str, clip: VideoClipEvidence, untrusted_data: str
    ) -> WireOutcome[GeminiStageResult]: ...


class SpecialistCaller(Protocol):
    """``GlmVisualSpecialistProvider`` shape: video-only trace observations."""

    pin: EditorialPinV2

    def observe_with_trace(
        self, clip: VideoClipEvidence, untrusted_data: str
    ) -> WireOutcome[GlmClipObservation]: ...


class ClipSource(Protocol):
    """``ClipExtractor`` shape: both clips for one exact window."""

    def extract(self, window: ReviewWindow) -> ClipEvidencePair: ...


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class SpecialistGap(StrictModel):
    """One advisory specialist target that failed after the bounded retry."""

    start_frame: Frame
    end_frame: Frame
    code: str = Field(min_length=1, strict=True)

    def overlaps(self, window: ReviewWindow) -> bool:
        return (
            int(self.start_frame) < int(window.end_frame)
            and int(self.end_frame) > int(window.start_frame)
        )


class VideoUnderstandingRequest(StrictModel):
    """Typed orchestration inputs (runtime state, never an artifact).

    GLM specialist targets come ONLY from the deterministic progressive
    windows plus the reduce stage's own typed requests — caller-supplied
    windows cannot masquerade as provider output, so no such field exists.
    """

    episode_id: Identifier
    source_duration_frames: Frame
    frame_rate: RationalFrameRate = Field(
        default_factory=lambda: RationalFrameRate(num=30, den=1)
    )
    known_shot_ids: frozenset[str] = Field(default_factory=frozenset)
    progressive_windows: Annotated[
        tuple[DeepReviewWindow, ...], BeforeValidator(_to_tuple)
    ] = Field(default_factory=tuple)
    speech_boundaries: Annotated[tuple[Frame, ...], BeforeValidator(_to_tuple)] = Field(
        default_factory=tuple
    )
    lead_policy: LeadMapWindowPolicy = Field(default_factory=LeadMapWindowPolicy)


@dataclass(frozen=True, slots=True)
class VideoUnderstandingDeps:
    """The wiring one episode constructs once (no framework, just facts)."""

    gemini: GeminiStageCaller
    specialist: SpecialistCaller
    clips: ClipSource
    transcripts: TranscriptLookup
    audio: AudioContextSource


@dataclass(frozen=True, slots=True)
class LocalStageRecord:
    """One completed local-map stage kept for reduce/fusion assembly."""

    window: ReviewWindow
    pair: ClipEvidencePair
    result: GeminiClipReview
    stage: ReviewStageLineage
    transcript_ids: tuple[str, ...]
    audio: AudioContext


class VideoUnderstandingResult(StrictModel):
    """Rebuildable orchestration outputs (the reviews are the artifacts)."""

    reviews: tuple[MomentDeepReviewV1, ...]
    lead_map_budget: AnalysisBudgetV1
    targeted_budget: AnalysisBudgetV1
    reduce_stage: ReviewStageLineage
    specialist_stages: Annotated[
        tuple[ReviewStageLineage, ...], BeforeValidator(_to_tuple)
    ] = Field(default_factory=tuple)
    deferred: Annotated[tuple[DeepReviewWindow, ...], BeforeValidator(_to_tuple)] = Field(
        default_factory=tuple
    )


__all__ = [
    "VIDEO_UNDERSTANDING_TOOL",
    "ClipSource",
    "GeminiStageCaller",
    "HardSpecialistTargetError",
    "LocalStageRecord",
    "SpecialistCaller",
    "SpecialistGap",
    "StageRangeMismatchError",
    "VideoUnderstandingDeps",
    "VideoUnderstandingError",
    "VideoUnderstandingRequest",
    "VideoUnderstandingResult",
]
