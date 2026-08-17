"""Artifact models for the minimum visual checks (Todo 35).

These are EVIDENCE checks (facts with provenance), not candidates, and they
are never selection inputs: ``evidence_only`` is the literal ``True`` and no
model exports any selection/apply surface. The artifact cross-checks every
span against the decode binding (traceability + frame counts) and binds all
canonical content into its ``content_hash`` — a stale or tampered binding is
a validation error by construction.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Final, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.analyze.analysis_models import AnalyzeError
from services.analyze.visual_constants import ANALYZER_NAME, ANALYZER_VERSION
from services.analyze.visual_results import (  # noqa: TC001 (pydantic runtime fields)
    BlackSpanResult,
    BlurSpanResult,
    CheckProvenance,
    ExposureSpanResult,
    SceneChangeResult,
)
from services.contracts.primitives import (
    ArtifactEnvelope,
    ArtifactRef,
    Producer,
    Sha256,
    StrictModel,
)


class VisualDecodeError(AnalyzeError):
    """The media failed bounded pinned-ffmpeg decode validation."""

    label = "corrupt_decode"


class VisualStreamFacts(StrictModel):
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)
    rate_num: int = Field(gt=0, strict=True)
    rate_den: int = Field(gt=0, strict=True)
    time_base_num: int = Field(gt=0, strict=True)
    time_base_den: int = Field(gt=0, strict=True)
    frame_count: int = Field(gt=0, strict=True)


class Pts(StrictModel):
    """Exact presentation timestamp in stream time_base units, reduced."""

    num: int = Field(ge=0, strict=True)
    den: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_reduced(self) -> Pts:
        if math.gcd(self.num, self.den) != 1 or (self.num == 0 and self.den != 1):
            raise PydanticCustomError("pts_not_reduced", "pts must be gcd-reduced canonical")
        return self


class DecodeBinding(StrictModel):
    media_path: str
    media_sha256: Sha256
    facts: VisualStreamFacts
    decode_w: int = Field(gt=0, strict=True)
    decode_h: int = Field(gt=0, strict=True)
    decode_filter: str = Field(min_length=1, strict=True)
    ffmpeg_sha256: Sha256
    ffprobe_sha256: Sha256


class FrameFact(StrictModel):
    frame_index: int = Field(ge=0, strict=True)
    pts: Pts
    mean_luma_m: int = Field(ge=0, le=1000, strict=True)
    black_pixel_fraction_m: int = Field(ge=0, le=1000, strict=True)
    clipped_high_fraction_m: int = Field(ge=0, le=1000, strict=True)
    diff_prev_m: int = Field(ge=0, le=1000, strict=True)
    laplacian_mean_square: int = Field(ge=0, strict=True)


class SheetRecord(StrictModel):
    path: str
    sha256: Sha256
    frame_indexes: tuple[int, ...] = Field(min_length=1)
    cols: int = Field(gt=0, strict=True)
    rows: int = Field(gt=0, strict=True)
    thumb_w: int = Field(gt=0, strict=True)
    thumb_h: int = Field(gt=0, strict=True)
    generator: str = Field(min_length=1, strict=True)
    cadence_frames: int = Field(gt=0, strict=True)
    rebuildable: Literal[True] = True
    provenance: CheckProvenance


class VisualAnalysisArtifact(ArtifactEnvelope[Literal["analysis_visual_minimum"]]):
    media: DecodeBinding
    frames: tuple[FrameFact, ...] = Field(min_length=1)
    scene_changes: tuple[SceneChangeResult, ...]
    black_spans: tuple[BlackSpanResult, ...]
    blur_spans: tuple[BlurSpanResult, ...]
    exposure_spans: tuple[ExposureSpanResult, ...]
    sheets: tuple[SheetRecord, ...]
    fixture_only: bool = False
    evidence_only: Literal[True] = True

    @model_validator(mode="after")
    def require_traceable_and_hashed(self) -> VisualAnalysisArtifact:
        count = self.media.facts.frame_count
        if len(self.frames) != count or any(
            fact.frame_index != index for index, fact in enumerate(self.frames)
        ):
            raise PydanticCustomError(
                "untraceable_frame", "frames must be exactly the decoded 0..n-1 sequence"
            )
        binding_sha = decode_binding_hash(self.media)
        groups = (self.scene_changes, self.black_spans, self.blur_spans, self.exposure_spans)
        for group in groups:
            if any(span.decode_frame_count != count for span in group):
                raise PydanticCustomError(
                    "untraceable_frame", "span decode_frame_count drifts from the binding"
                )
        for item in (*self.scene_changes, *self.black_spans, *self.blur_spans,
                     *self.exposure_spans, *self.sheets):
            if item.provenance.decode_binding_sha256 != binding_sha:
                raise PydanticCustomError(
                    "untraceable_frame", "provenance does not match the decode binding"
                )
        expected = visual_content_hash(
            self.media,
            self.frames,
            self.scene_changes,
            self.black_spans,
            self.blur_spans,
            self.exposure_spans,
            self.sheets,
            fixture_only=self.fixture_only,
        )
        if self.content_hash != expected:
            raise PydanticCustomError(
                "content_hash_mismatch", "content_hash must bind the canonical content bytes"
            )
        return self


def visual_content_hash(  # noqa: PLR0913, PLR0917 (envelope content fields are the record)
    media: DecodeBinding,
    frames: tuple[FrameFact, ...],
    scene_changes: tuple[SceneChangeResult, ...],
    black_spans: tuple[BlackSpanResult, ...],
    blur_spans: tuple[BlurSpanResult, ...],
    exposure_spans: tuple[ExposureSpanResult, ...],
    sheets: tuple[SheetRecord, ...],
    *,
    fixture_only: bool,
) -> str:
    payload = {
        "media": media.model_dump(mode="json"),
        "frames": [fact.model_dump(mode="json") for fact in frames],
        "scene_changes": [item.model_dump(mode="json") for item in scene_changes],
        "black_spans": [item.model_dump(mode="json") for item in black_spans],
        "blur_spans": [item.model_dump(mode="json") for item in blur_spans],
        "exposure_spans": [item.model_dump(mode="json") for item in exposure_spans],
        "sheets": [sheet.model_dump(mode="json") for sheet in sheets],
        "fixture_only": fixture_only,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def decode_binding_hash(binding: DecodeBinding) -> str:
    canonical = json.dumps(
        binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


VISUAL_PRODUCER: Final[Producer] = Producer(name=ANALYZER_NAME, version=ANALYZER_VERSION)

FORBIDDEN_SELECTION_EXPORTS: Final = (
    "select",
    "auto_select",
    "rank",
    "rank_candidates",
    "score_candidate",
    "selection_plan",
    "apply_to_plan",
)


def visual_input_refs(media_sha256: Sha256) -> tuple[ArtifactRef, ...]:
    return (ArtifactRef(artifact_id="edit-source-video", sha256=media_sha256),)
