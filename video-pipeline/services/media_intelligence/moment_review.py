# allow: SIZE_OK — PRD 7.6 artifact module (data models + builders + synthetic
# providers in one file per plan task 17); mirrors the budget/progressive split
# where the model surface dominates the line count.
"""``MomentDeepReviewV1`` — evidence-only moment deep review (PRD 7.6, plan 6.6).

A deep-reviewed moment is narrower and richer than a shot summary: exact
source window, neighbouring-shot context, dense frame bundle, overlapping
transcript refs, audio context, action/reaction/timing assessment, best
sub-span, keep/remove rationale candidates, cut handles, confidence and
lineage.  A Moment Deep Review NEVER commits an edit — this module exposes
no plan/timeline mutation API of any kind (statically asserted in tests);
it only upgrades the evidence available to Editorial Intelligence.

Determinism: ``review_id`` is derived from ``(episode_id, window)`` alone
(shot_identity-style SHA-256; same window + episode -> same id, independent
of provider), and every builder here is deterministic — no clocks, random,
or UUIDs — so identical inputs produce byte-identical canonical output.

The assessment produced by :func:`execute_review` is a documented
deterministic placeholder heuristic; real editorial judgment arrives with
the editorial layer (per PRD 7.6 the LLM/vision provider plugs in later).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Final, Literal, Protocol

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Frame, Identifier, StrictModel
from services.foundation_io import canonical_model_bytes

_IDENTIFIER_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _to_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class MomentReviewError(ValueError):
    """Base moment deep-review failure."""

    LABEL = "moment_review_error"


class WindowOutOfBoundsError(MomentReviewError):
    """The review window (or its best sub-span) exceeds the source bounds."""

    LABEL = "window_out_of_bounds"


class FrameOutsideWindowError(MomentReviewError):
    """A frame-bundle entry lies outside the half-open review window."""

    LABEL = "frame_outside_window"


class UnknownNeighborError(MomentReviewError):
    """A neighbouring-shot reference is absent from the known shot ids."""

    LABEL = "unknown_neighbor"


# ---------------------------------------------------------------------------
# Artifact models
# ---------------------------------------------------------------------------


class ReviewWindow(StrictModel):
    """Half-open ``[start_frame, end_frame)`` window under deep review."""

    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> ReviewWindow:
        if self.end_frame < self.start_frame:
            raise PydanticCustomError("span_inverted", "end_frame must be >= start_frame")
        return self


class NeighboringContext(StrictModel):
    """Adjacent-shot context attached to a review, when known."""

    prev_shot_id: Identifier | None = None
    next_shot_id: Identifier | None = None


class FrameBundleEntry(StrictModel):
    """One dense-evidence frame plus its evidence ref."""

    frame: Frame
    ref: Annotated[str, Field(min_length=1, strict=True)]


class TranscriptRef(StrictModel):
    """Transcript segment span used for overlap lookup."""

    segment_id: Identifier
    start_frame: Frame
    end_frame: Frame


class AudioContext(StrictModel):
    """Acoustic context when measurable (energy/silence/reaction/ambient)."""

    ambient_type: str | None = None
    loudness_db: float | None = None
    energy: float | None = None
    note: str | None = None


class ReviewEvidence(StrictModel):
    """Short-window evidence bundle backing the assessment."""

    frame_bundle: Annotated[tuple[FrameBundleEntry, ...], BeforeValidator(_to_tuple)] = Field(
        default_factory=tuple
    )
    transcript_refs: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)] = Field(
        default_factory=tuple
    )
    audio_context: AudioContext


class BestSubSpan(StrictModel):
    """Candidate best sub-span (half-open), not merely the best shot."""

    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> BestSubSpan:
        if self.end_frame < self.start_frame:
            raise PydanticCustomError("span_inverted", "end_frame must be >= start_frame")
        return self


class MomentAssessment(StrictModel):
    """Assessment fields — rationale candidates, never edit decisions."""

    subject_action_evolution: Annotated[str, Field(min_length=1, strict=True)]
    reaction_notes: Annotated[str, Field(min_length=1, strict=True)]
    timing_notes: Annotated[str, Field(min_length=1, strict=True)]
    best_sub_span: BestSubSpan
    keep_rationale_candidates: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)]
    remove_rationale_candidates: Annotated[tuple[str, ...], BeforeValidator(_to_tuple)]
    cut_in_handle: Annotated[str, Field(min_length=1, strict=True)]
    cut_out_handle: Annotated[str, Field(min_length=1, strict=True)]


class ReviewConfidence(StrictModel):
    """Overall plus per-field confidence, each in [0, 1]."""

    overall: float = Field(ge=0.0, le=1.0)
    subject_action_evolution: float = Field(ge=0.0, le=1.0)
    reaction_notes: float = Field(ge=0.0, le=1.0)
    timing_notes: float = Field(ge=0.0, le=1.0)
    best_sub_span: float = Field(ge=0.0, le=1.0)


class ReviewLineage(StrictModel):
    """Provider/version/tool/cost lineage (PRD 7.5 records analysis cost)."""

    provider: Annotated[str, Field(min_length=1, strict=True)]
    provider_version: Annotated[str, Field(min_length=1, strict=True)]
    tool: Annotated[str, Field(min_length=1, strict=True)]
    cost: float | None = Field(default=None, ge=0.0)


class MomentDeepReviewV1(StrictModel):
    """Moment deep-review evidence artifact (``moment-deep-review-v1``)."""

    schema_version: Literal["moment-deep-review-v1"] = "moment-deep-review-v1"
    review_id: Identifier
    episode_id: Identifier
    source_window: ReviewWindow
    neighboring_context: NeighboringContext = Field(default_factory=NeighboringContext)
    evidence: ReviewEvidence
    assessment: MomentAssessment
    confidence: ReviewConfidence
    lineage: ReviewLineage


# ---------------------------------------------------------------------------
# Deterministic identity
# ---------------------------------------------------------------------------


def derive_moment_review_id(episode_id: str, start_frame: int, end_frame: int) -> str:
    """Derive the deterministic review id for ``(episode_id, window)``.

    Shot-identity-style hashing: canonical ``f"{episode}:{start}:{end}"`` is
    SHA-256-hashed and the first 16 hex characters become ``moment-<hex>``.
    Provider-independent on purpose — the id names the reviewed moment, so a
    re-review of the same window with a different provider yields the same id.
    """

    if not _IDENTIFIER_RE.match(episode_id):
        raise MomentReviewError(f"episode_id must match Identifier pattern: {episode_id!r}")
    if not isinstance(start_frame, int) or not isinstance(end_frame, int):
        raise MomentReviewError("frames must be strict integers")
    if start_frame < 0 or end_frame < 0:
        raise MomentReviewError("frames must be >= 0")
    if end_frame < start_frame:
        raise MomentReviewError("end_frame must be >= start_frame")

    canonical = f"{episode_id}:{start_frame}:{end_frame}"
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    review_id = f"moment-{digest}"
    if not _IDENTIFIER_RE.match(review_id):
        raise MomentReviewError(f"derived review_id invalid: {review_id!r}")
    return review_id


def review_content_sha(review: MomentDeepReviewV1) -> str:
    """SHA-256 of the canonical review bytes (index lineage, v1 discipline)."""

    return hashlib.sha256(canonical_model_bytes(review)).hexdigest()


def review_rows(
    reviews: Sequence[MomentDeepReviewV1],
) -> tuple[tuple[object, ...], ...]:
    """Deterministic ``mi_moment_reviews`` row values, sorted for the index.

    Row shape (matches the additive v2 DDL): episode_id, review_id,
    artifact_sha, start_frame, end_frame, prev_shot_id, next_shot_id,
    overall_confidence, provider, provider_version, tool.
    """

    ordered = sorted(
        reviews,
        key=lambda review: (
            int(review.source_window.start_frame),
            int(review.source_window.end_frame),
            str(review.review_id),
            review_content_sha(review),
        ),
    )
    return tuple(
        (
            str(review.episode_id),
            str(review.review_id),
            review_content_sha(review),
            int(review.source_window.start_frame),
            int(review.source_window.end_frame),
            review.neighboring_context.prev_shot_id,
            review.neighboring_context.next_shot_id,
            review.confidence.overall,
            review.lineage.provider,
            review.lineage.provider_version,
            review.lineage.tool,
        )
        for review in ordered
    )


# ---------------------------------------------------------------------------
# record_review — validating builder
# ---------------------------------------------------------------------------


class ReviewRecordRequest(StrictModel):
    """Typed inputs for :func:`record_review`."""

    episode_id: Identifier
    source_duration_frames: Frame
    known_shot_ids: frozenset[str] = Field(default_factory=frozenset)
    window: ReviewWindow
    neighboring_context: NeighboringContext = Field(default_factory=NeighboringContext)
    evidence: ReviewEvidence
    assessment: MomentAssessment
    confidence: ReviewConfidence
    lineage: ReviewLineage


def record_review(request: ReviewRecordRequest) -> MomentDeepReviewV1:
    """Validate and commit a review as a ``MomentDeepReviewV1`` artifact.

    Typed rejections: ``WindowOutOfBoundsError`` when the window (or its
    best sub-span) exceeds the source, ``FrameOutsideWindowError`` when a
    frame-bundle entry falls outside the half-open window, and
    ``UnknownNeighborError`` for neighbour refs absent from the known shots.
    """

    duration = int(request.source_duration_frames)
    if duration <= 0:
        raise MomentReviewError("source duration must be > 0 frames for a review")
    if int(request.window.end_frame) > duration:
        raise WindowOutOfBoundsError(
            f"window end_frame {request.window.end_frame} exceeds source duration {duration}"
        )

    start = int(request.window.start_frame)
    end = int(request.window.end_frame)
    for entry in request.evidence.frame_bundle:
        if not start <= int(entry.frame) < end:
            raise FrameOutsideWindowError(
                f"frame {entry.frame} outside half-open window [{start}, {end})"
            )

    sub = request.assessment.best_sub_span
    if not start <= int(sub.start_frame) <= int(sub.end_frame) <= end:
        raise WindowOutOfBoundsError(
            f"best sub-span [{sub.start_frame}, {sub.end_frame}] outside window "
            f"[{start}, {end})"
        )

    for side in ("prev_shot_id", "next_shot_id"):
        neighbor = getattr(request.neighboring_context, side)
        if neighbor is not None and neighbor not in request.known_shot_ids:
            raise UnknownNeighborError(f"{side} {neighbor!r} is not a known shot id")

    return MomentDeepReviewV1(
        review_id=derive_moment_review_id(
            request.episode_id, request.window.start_frame, request.window.end_frame
        ),
        episode_id=request.episode_id,
        source_window=request.window,
        neighboring_context=request.neighboring_context,
        evidence=request.evidence,
        assessment=request.assessment,
        confidence=request.confidence,
        lineage=request.lineage,
    )


# ---------------------------------------------------------------------------
# Review executor seam — injectable providers (synthetic for now)
# ---------------------------------------------------------------------------


class DenseFrameExtractor(Protocol):
    """Dense frame-bundle extraction over a review window."""

    def extract(self, window: ReviewWindow) -> tuple[FrameBundleEntry, ...]: ...


class TranscriptLookup(Protocol):
    """Transcript segment ids overlapping a review window."""

    def overlapping(self, window: ReviewWindow) -> tuple[str, ...]: ...


class AudioContextSource(Protocol):
    """Acoustic context for a review window."""

    def context(self, window: ReviewWindow) -> AudioContext: ...


@dataclass(frozen=True, slots=True)
class ReviewProviders:
    """Injectable provider bundle; the LLM/vision provider plugs in later."""

    frames: DenseFrameExtractor
    transcripts: TranscriptLookup
    audio: AudioContextSource
    lineage: ReviewLineage


class ReviewExecutionContext(StrictModel):
    """Episode facts the executor needs to validate and stamp a review."""

    episode_id: Identifier
    source_duration_frames: Frame
    known_shot_ids: frozenset[str] = Field(default_factory=frozenset)
    neighbors: NeighboringContext | None = None


@dataclass(frozen=True, slots=True)
class SyntheticFrameExtractor:
    """Deterministic dense sampler: evenly spaced frames, ``density`` at most."""

    density: int = 4

    def extract(self, window: ReviewWindow) -> tuple[FrameBundleEntry, ...]:
        if self.density < 1:
            raise MomentReviewError("density must be >= 1")
        span = int(window.end_frame) - int(window.start_frame)
        count = min(self.density, span)
        if count <= 0:
            return ()
        return tuple(
            FrameBundleEntry(
                frame=int(window.start_frame) + (index * span) // count,
                ref=f"synthetic://frame/{int(window.start_frame) + (index * span) // count}",
            )
            for index in range(count)
        )


@dataclass(frozen=True, slots=True)
class SyntheticTranscriptLookup:
    """Deterministic overlap lookup over supplied transcript refs."""

    segments: tuple[TranscriptRef, ...] = ()

    def overlapping(self, window: ReviewWindow) -> tuple[str, ...]:
        return tuple(
            sorted(
                str(segment.segment_id)
                for segment in self.segments
                if int(segment.start_frame) < int(window.end_frame)
                and int(segment.end_frame) > int(window.start_frame)
            )
        )


@dataclass(frozen=True, slots=True)
class SyntheticAudioContext:
    """Deterministic ambient note derived from the window geometry."""

    def context(self, window: ReviewWindow) -> AudioContext:
        span = int(window.end_frame) - int(window.start_frame)
        return AudioContext(
            ambient_type="synthetic", note=f"synthetic ambient across {span} frames"
        )


def _heuristic_assessment(window: ReviewWindow, evidence: ReviewEvidence) -> MomentAssessment:
    """DETERMINISTIC PLACEHOLDER heuristic — documented, not editorial judgment.

    Real action/reaction/timing assessment arrives with the editorial layer's
    LLM/vision provider (PRD 7.6); until then this derives stable strings and
    a middle-third best sub-span from the window geometry and evidence alone.
    """

    start = int(window.start_frame)
    end = int(window.end_frame)
    span = end - start
    third = span // 3
    return MomentAssessment(
        subject_action_evolution=(
            f"synthetic: {len(evidence.frame_bundle)} dense frames sampled across "
            f"[{start}, {end})"
        ),
        reaction_notes="synthetic: no reaction signal extracted by placeholder heuristic",
        timing_notes=f"synthetic: window spans {span} frames; best sub-span on the middle third",
        best_sub_span=BestSubSpan(start_frame=start + third, end_frame=end - third),
        keep_rationale_candidates=("synthetic: dense frame coverage supports the candidate",),
        remove_rationale_candidates=(
            () if evidence.transcript_refs else ("synthetic: no transcript overlap in window",)
        ),
        cut_in_handle=f"cut-in handle before frame {start + third}",
        cut_out_handle=f"cut-out handle after frame {end - third}",
    )


def execute_review(
    window: ReviewWindow,
    providers: ReviewProviders,
    *,
    context: ReviewExecutionContext,
) -> MomentDeepReviewV1:
    """Assemble evidence via the provider bundle and record the review.

    Evidence assembly is provider-driven and fully deterministic under the
    synthetic providers; the assessment is the documented placeholder
    heuristic.  Validation and id stamping delegate to :func:`record_review`
    so the executor can never emit an unvalidated review.
    """

    evidence = ReviewEvidence(
        frame_bundle=providers.frames.extract(window),
        transcript_refs=providers.transcripts.overlapping(window),
        audio_context=providers.audio.context(window),
    )
    return record_review(
        ReviewRecordRequest(
            episode_id=context.episode_id,
            source_duration_frames=context.source_duration_frames,
            known_shot_ids=context.known_shot_ids,
            window=window,
            neighboring_context=context.neighbors or NeighboringContext(),
            evidence=evidence,
            assessment=_heuristic_assessment(window, evidence),
            confidence=ReviewConfidence(
                overall=0.5,
                subject_action_evolution=0.5,
                reaction_notes=0.5,
                timing_notes=0.5,
                best_sub_span=0.5,
            ),
            lineage=providers.lineage,
        )
    )


__all__ = [
    "AudioContext",
    "AudioContextSource",
    "BestSubSpan",
    "DenseFrameExtractor",
    "FrameBundleEntry",
    "FrameOutsideWindowError",
    "MomentAssessment",
    "MomentDeepReviewV1",
    "MomentReviewError",
    "NeighboringContext",
    "ReviewConfidence",
    "ReviewEvidence",
    "ReviewExecutionContext",
    "ReviewLineage",
    "ReviewProviders",
    "ReviewRecordRequest",
    "ReviewWindow",
    "SyntheticAudioContext",
    "SyntheticFrameExtractor",
    "SyntheticTranscriptLookup",
    "TranscriptLookup",
    "TranscriptRef",
    "UnknownNeighborError",
    "WindowOutOfBoundsError",
    "derive_moment_review_id",
    "execute_review",
    "record_review",
    "review_content_sha",
    "review_rows",
]
