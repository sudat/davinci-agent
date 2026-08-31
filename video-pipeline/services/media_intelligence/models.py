"""Canonical MediaIntelligenceArtifact v2.

Coordinate contract:
    Every ``start_frame`` / ``end_frame`` / ``frame`` value is an Edit
    Source Frame — an integer frame index on the CFR Edit Mezzanine at the
    episode's edit rate.  ``strict=True`` integer validation is intentional
    (``1.5`` must be rejected) and mirrors ``Frame`` from
    ``services.contracts.primitives`` without importing conform internals.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Frame, Identifier, SourceId, StrictModel, to_tuple

# ---------------------------------------------------------------------------
# PRD 7.2 evidence-category → field mapping (test hook for category coverage)
# ---------------------------------------------------------------------------

PRD_7_2_FIELD_MAPPING: Final[dict[str, str]] = {
    "transcript and word/segment timing": "Shot.transcript_segments, Shot.word_timings",
    "speaker identity when available": "Shot.speaker_info",
    "silence and filler evidence": "Shot.silence_segments, Shot.filler_words",
    "loudness/energy/ambient measurements": "Shot.audio_measurements",
    "shot boundaries": "Shot.source_span, Shot.shot_boundary",
    "shot size and framing": "Shot.visual.shot_size, Shot.visual.framing, Shot.framing_detail",
    "camera movement": "Shot.visual.camera_motion",
    "primary subject and action": "Shot.subject_action",
    "location and visible text": "Shot.location, Shot.visible_texts",
    "visual quality problems": "Shot.visual.quality_flags, Shot.visual_quality_detail",
    "editorial role": "Shot.editorial.role",
    "select potential": "Shot.editorial.select_potential",
    "best moment candidate": "Shot.editorial.best_moment",
    "pacing and stillness type": "Shot.editorial.pacing, Shot.editorial.stillness_type",
    "cut-in/cut-out quality": "Shot.editorial.cuttability",
    "visual similarity/embedding references": "Shot.similarity_refs",
    "objects or product references": "Shot.object_refs, Shot.product_refs",
    "face/reaction cues when confidently available": "Shot.face_reaction_cues",
    "provenance and confidence per field": "Shot.confidence, Shot.provenance",
}


# ---------------------------------------------------------------------------
# Primitive sub-models (all StrictModel → frozen, strict, extra=forbid)
# ---------------------------------------------------------------------------


class EditSourceSpan(StrictModel):
    """Half-open ``[start_frame, end_frame)`` on the Edit Source."""

    start_frame: Frame
    end_frame: Frame

    @model_validator(mode="after")
    def require_forward(self) -> EditSourceSpan:
        if self.end_frame < self.start_frame:
            raise PydanticCustomError(
                "span_inverted", "end_frame must be >= start_frame"
            )
        return self


class ShotBestMoment(StrictModel):
    frame: Frame
    why: Annotated[str, Field(min_length=1, strict=True)]


class ShotCuttability(StrictModel):
    """``in`` is a Python keyword — stored as ``in_`` with alias ``in``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    in_: Annotated[str, Field(min_length=1, strict=True, alias="in")]
    out: Annotated[str, Field(min_length=1, strict=True)]


class ShotConfidence(StrictModel):
    editorial: Annotated[str, Field(min_length=1, strict=True)]
    visual: Annotated[str, Field(min_length=1, strict=True)]


class ShotVisual(StrictModel):
    shot_size: Annotated[str, Field(min_length=1, strict=True)]
    camera_motion: Annotated[str, Field(min_length=1, strict=True)]
    quality_flags: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )
    framing: str | None = None


class ShotEditorial(StrictModel):
    role: Annotated[str, Field(min_length=1, strict=True)]
    select_potential: Annotated[str, Field(min_length=1, strict=True)]
    best_moment: ShotBestMoment
    pacing: Annotated[str, Field(min_length=1, strict=True)]
    cuttability: ShotCuttability
    stillness_type: str | None = None


# --- optional evidence carriers (one per PRD 7.2 grouping) ---


class TranscriptWordTiming(StrictModel):
    word: Annotated[str, Field(min_length=1, strict=True)]
    start_frame: Frame
    end_frame: Frame


class TranscriptSegment(StrictModel):
    segment_id: Identifier
    text: Annotated[str, Field(min_length=1, strict=True)]
    start_frame: Frame
    end_frame: Frame
    words: Annotated[tuple[TranscriptWordTiming, ...], BeforeValidator(to_tuple)] | None = None


class SpeakerInfo(StrictModel):
    speaker_id: Identifier
    display_name: str | None = None


class SilenceSegment(StrictModel):
    start_frame: Frame
    end_frame: Frame
    kind: str | None = None


class FillerWord(StrictModel):
    word: Annotated[str, Field(min_length=1, strict=True)]
    frame: Frame


class AudioMeasurements(StrictModel):
    loudness_db: float | None = None
    energy: float | None = None
    ambient_type: str | None = None


class ShotBoundaryInfo(StrictModel):
    boundary_type: Annotated[str, Field(min_length=1, strict=True)]
    confidence: str | None = None


class FramingInfo(StrictModel):
    framing: Annotated[str, Field(min_length=1, strict=True)]
    description: str | None = None


class SubjectAction(StrictModel):
    primary_subject: Annotated[str, Field(min_length=1, strict=True)]
    action: Annotated[str, Field(min_length=1, strict=True)]


class LocationInfo(StrictModel):
    location: Annotated[str, Field(min_length=1, strict=True)]
    description: str | None = None


class VisibleText(StrictModel):
    text: Annotated[str, Field(min_length=1, strict=True)]
    frame: Frame | None = None


class VisualQualityDetail(StrictModel):
    issue: Annotated[str, Field(min_length=1, strict=True)]
    severity: str | None = None


class SimilarityRef(StrictModel):
    ref_shot_id: Identifier
    score: float | None = None


class ObjectRef(StrictModel):
    label: Annotated[str, Field(min_length=1, strict=True)]
    frame: Frame | None = None


class ProductRef(StrictModel):
    product_id: Identifier
    frame: Frame | None = None


class FaceReactionCue(StrictModel):
    cue: Annotated[str, Field(min_length=1, strict=True)]
    frame: Frame | None = None
    confidence: str | None = None


class ProvenanceRecord(StrictModel):
    field: Annotated[str, Field(min_length=1, strict=True)]
    provider: Annotated[str, Field(min_length=1, strict=True)]
    provider_version: str | None = None
    confidence: str | None = None


# ---------------------------------------------------------------------------
# Top-level carriers
# ---------------------------------------------------------------------------


class MediaSource(StrictModel):
    source_id: SourceId
    duration_frames: Frame | None = None
    path: str | None = None


class Shot(StrictModel):
    shot_id: Identifier
    source_span: EditSourceSpan
    description: Annotated[str, Field(min_length=1, strict=True)]
    visual: ShotVisual
    editorial: ShotEditorial
    transcript_refs: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )
    evidence_refs: Annotated[tuple[str, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )
    confidence: ShotConfidence

    # --- optional PRD 7.2 evidence (additive, all None by default) ---
    transcript_segments: Annotated[
        tuple[TranscriptSegment, ...], BeforeValidator(to_tuple)
    ] | None = None
    word_timings: Annotated[
        tuple[TranscriptWordTiming, ...], BeforeValidator(to_tuple)
    ] | None = None
    speaker_info: SpeakerInfo | None = None
    silence_segments: Annotated[
        tuple[SilenceSegment, ...], BeforeValidator(to_tuple)
    ] | None = None
    filler_words: Annotated[
        tuple[FillerWord, ...], BeforeValidator(to_tuple)
    ] | None = None
    audio_measurements: AudioMeasurements | None = None
    shot_boundary: ShotBoundaryInfo | None = None
    framing_detail: FramingInfo | None = None
    subject_action: SubjectAction | None = None
    location: LocationInfo | None = None
    visible_texts: Annotated[
        tuple[VisibleText, ...], BeforeValidator(to_tuple)
    ] | None = None
    visual_quality_detail: VisualQualityDetail | None = None
    similarity_refs: Annotated[
        tuple[SimilarityRef, ...], BeforeValidator(to_tuple)
    ] | None = None
    object_refs: Annotated[
        tuple[ObjectRef, ...], BeforeValidator(to_tuple)
    ] | None = None
    product_refs: Annotated[
        tuple[ProductRef, ...], BeforeValidator(to_tuple)
    ] | None = None
    face_reaction_cues: Annotated[
        tuple[FaceReactionCue, ...], BeforeValidator(to_tuple)
    ] | None = None
    provenance: Annotated[
        tuple[ProvenanceRecord, ...], BeforeValidator(to_tuple)
    ] | None = None


class MediaIntelligenceArtifact(StrictModel):
    """Canonical multimodal intelligence artifact (media-intelligence-v2)."""

    schema_version: Literal["media-intelligence-v2"] = "media-intelligence-v2"
    episode_id: Identifier
    sources: Annotated[tuple[MediaSource, ...], BeforeValidator(to_tuple)] = Field(
        default_factory=tuple
    )
    shots: Annotated[tuple[Shot, ...], BeforeValidator(to_tuple)] = Field(default_factory=tuple)
