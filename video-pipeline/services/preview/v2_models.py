"""Shared Preview v2 artifact models (task 60; PRD 13.1).

Timeline IR v2 (task 31) and the subtitle plan (task 33) feed these; every
value is integer-exact (frames, sample-rate integers, sha256 hex) mirroring
the v1 preview model conventions. The v1 contracts module stays frozen —
these models are additive and only the v2 modules import them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Frame,
    Identifier,
    RationalFrameRate,
    Sha256,
    SourceId,
    StrictModel,
)
from services.preview.models import (  # noqa: TC001 (pydantic runtime)
    FfprobeSummary,
)


def _to_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


class SourceMediaEntryV2(StrictModel):
    """One Edit Source and the absolute media file that backs it."""

    source_id: SourceId
    media_path: str
    sha256: Sha256

    @field_validator("media_path")
    @classmethod
    def require_absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise PydanticCustomError("relative_path", "media bindings must be absolute paths")
        return value


class SourceMediaMapV2(StrictModel):
    """source_id -> media file mapping for the editorial render."""

    entries: Annotated[tuple[SourceMediaEntryV2, ...], BeforeValidator(_to_tuple)] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def require_unique_source_ids(self) -> SourceMediaMapV2:
        ids = [entry.source_id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("duplicate_source", "media map source ids are unique")
        return self

    def entry_for(self, source_id: str) -> SourceMediaEntryV2 | None:
        return next((entry for entry in self.entries if entry.source_id == source_id), None)


class PreviewFileV2(StrictModel):
    path: str
    sha256: Sha256
    size: int = Field(ge=0, strict=True)
    decoded_video_sha256: Sha256


class SubtitleSidecarV2(StrictModel):
    """The SRT sidecar kept beside the MP4 (approximate-subtitle rung)."""

    path: str
    sha256: Sha256
    size: int = Field(ge=0, strict=True)
    cue_count: int = Field(gt=0, strict=True)


class PreviewFlagV2(StrictModel):
    """One Moment Deep Review flag over a low-confidence region."""

    at_frame: Frame
    review_ref: Identifier
    reason: str = Field(min_length=1, strict=True)


CutInRoleV2 = Literal["b_roll", "insert", "still", "graphic"]


class CutInRecordV2(StrictModel):
    """A non-primary video placement burned into the preview as a cut-in."""

    item_id: Identifier
    role: CutInRoleV2
    start_frame: Frame
    end_frame: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_forward_span(self) -> CutInRecordV2:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("span_empty", "cut-in span is a non-empty half-open range")
        return self


ToneRoleV2 = Literal["music", "ambience", "sfx"]


class AudioPlaceholderV2(StrictModel):
    """A placeholder-tone audio placement (rough music/ambience/SFX cue)."""

    item_id: Identifier
    role: ToneRoleV2
    frequency_hz: int = Field(gt=0, strict=True)
    start_frame: Frame
    end_frame: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def require_forward_span(self) -> AudioPlaceholderV2:
        if self.end_frame <= self.start_frame:
            raise PydanticCustomError("span_empty", "tone span is a non-empty half-open range")
        return self


class FidelityNotesV2(StrictModel):
    """Documented rough-fidelity level of the editorial preview rung."""

    b_roll: Literal["full-frame-cut-in-overlay-pm1-frame-boundary"]
    subtitles: Literal["srt-sidecar-plus-soft-mov-text-no-burn-in"]
    titles: Literal["white-placeholder-card-no-burned-text"]
    music_ambience: Literal["sine-tone-placeholders-music440-ambience220-sfx880"]
    flags: Literal["manifest-plus-red-corner-marker-overlay"]
    determinism_policy: Literal["semantic-equivalence-h264-videotoolbox"]


class PreviewTraceV2(StrictModel):
    """Editorial Preview v2 trace manifest (PRD 13.1 rung)."""

    schema_version: Literal["preview-trace-v2"]
    episode_id: Identifier
    ir_sha256: Sha256
    rate: RationalFrameRate
    total_record_frames: int = Field(gt=0, strict=True)
    preview: PreviewFileV2
    subtitle_sidecar: SubtitleSidecarV2 | None = None
    flags: Annotated[tuple[PreviewFlagV2, ...], BeforeValidator(_to_tuple)] = ()
    cut_ins: Annotated[tuple[CutInRecordV2, ...], BeforeValidator(_to_tuple)] = ()
    audio_placeholders: Annotated[tuple[AudioPlaceholderV2, ...], BeforeValidator(_to_tuple)] = ()
    fidelity: FidelityNotesV2
    ffprobe_summary: FfprobeSummary


TONE_HZ_BY_ROLE: Final[dict[str, int]] = {"music": 440, "ambience": 220, "sfx": 880}


@dataclass(frozen=True, slots=True)
class ClipView:
    """A media-bound video placement (primary story clip or B-roll cut-in)."""

    item_id: str
    media_path: Path
    source_span: tuple[int, int]
    record_span: tuple[int, int]


@dataclass(frozen=True, slots=True)
class CardView:
    """A still/graphic placeholder-card placement (no media at this rung)."""

    item_id: str
    record_span: tuple[int, int]


@dataclass(frozen=True, slots=True)
class MediaAudioView:
    """A real-media audio placement (dialogue) in 48 kHz sample indices."""

    item_id: str
    media_path: Path
    source_start_sample: int
    source_end_sample: int
    delay_samples: int

    @property
    def length_samples(self) -> int:
        return self.source_end_sample - self.source_start_sample


@dataclass(frozen=True, slots=True)
class ToneAudioView:
    """A placeholder-tone audio placement (music/ambience/sfx)."""

    item_id: str
    tone_hz: int
    length_samples: int
    delay_samples: int


type AudioSegmentView = MediaAudioView | ToneAudioView


@dataclass(frozen=True, slots=True)
class EditorialLayoutV2:
    """Validated, media-bound, render-ready projection of a Timeline IR v2.

    ``media_cut_ins``/``card_views`` are ordered by (record start, item id);
    the builder composites cut-ins, then cards, then flag markers on top of
    the primary concat.
    """

    rate: RationalFrameRate
    samples_per_frame: int
    total_record_frames: int
    primary: tuple[ClipView, ...]
    media_cut_ins: tuple[ClipView, ...]
    card_views: tuple[CardView, ...]
    audio_segments: tuple[AudioSegmentView, ...]
    cut_in_records: tuple[CutInRecordV2, ...]
    audio_placeholders: tuple[AudioPlaceholderV2, ...]
    flags: tuple[PreviewFlagV2, ...]


__all__ = [
    "TONE_HZ_BY_ROLE",
    "AudioPlaceholderV2",
    "AudioSegmentView",
    "CardView",
    "ClipView",
    "CutInRecordV2",
    "CutInRoleV2",
    "EditorialLayoutV2",
    "FidelityNotesV2",
    "MediaAudioView",
    "PreviewFileV2",
    "PreviewFlagV2",
    "PreviewTraceV2",
    "SourceMediaEntryV2",
    "SourceMediaMapV2",
    "SubtitleSidecarV2",
    "ToneAudioView",
    "ToneRoleV2",
]
