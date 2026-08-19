"""Strict models for the Phase-2 FIXED presentation section (Todo 49).

Everything here is the frozen Phase-2 surface only: the fixed subtitle style
(external rung — the font family is recorded as EVIDENCE, never applied as an
instruction), the dialogue/ambient role table with role-separated Resolve
audio tracks, the 0A-verified render contract (aac / 48 kHz), and media-backed
intro/outro assets whose provenance {path, sha256, license_ref} is mandatory.
Phase-3 concerns (style profiles, BGM/SE, Fusion/Fairlight processing, camera
or channel profiles) are absent BY DESIGN: the section model forbids them and
:mod:`services.resolve_adapter.presentation_baseline` refuses them with a
typed scope error.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, Sha256, StrictModel

type AudioRole = Literal["dialogue", "ambient"]


class AssetProvenance(StrictModel):
    """Mandatory provenance for every media-backed presentation asset."""

    path: str = Field(min_length=1)
    sha256: Sha256
    license_ref: str = Field(min_length=1)


class AudioRoleEntry(StrictModel):
    """One source's role label with the analysis artifact it derives from."""

    source_id: Identifier
    role: AudioRole
    analysis_sha256: Sha256


class FixedAudioPolicy(StrictModel):
    """Role-separated audio tracks: dialogue and ambient never share a track."""

    strategy: Literal["dialogue-ambient-role-tracks-v1"]
    dialogue_resolve_track: int = Field(default=1, gt=0, strict=True)
    ambient_resolve_track: int = Field(default=2, gt=0, strict=True)
    roles: tuple[AudioRoleEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_role_separated_tracks(self) -> FixedAudioPolicy:
        if self.dialogue_resolve_track == self.ambient_resolve_track:
            raise PydanticCustomError(
                "role_conflation",
                "dialogue and ambient must never share resolve audio track {track}",
                {"track": self.dialogue_resolve_track},
            )
        seen = [entry.source_id for entry in self.roles]
        if len(set(seen)) != len(seen):
            raise PydanticCustomError("role_duplicate", "one role per source")
        return self


class FixedSubtitleStyle(StrictModel):
    """The Todo-17 fixed external-rung subtitle style, recorded honestly."""

    rung: Literal["external"] = "external"
    style_ref: Identifier
    font_family: str = Field(min_length=1)
    font_family_status: Literal["evidence-only"] = "evidence-only"
    position: Literal["bottom-center-safe-area"] = "bottom-center-safe-area"
    safe_area_compliant: Literal[True] = True


class FixedRenderPreset(StrictModel):
    """The 0A-verified render contract this baseline is frozen to."""

    preset_id: Literal["phase-0a-h264-aac-v1"]
    audio_codec: str = Field(min_length=1)
    audio_sample_rate: int = Field(gt=0, strict=True)
    audio_channels: int = Field(gt=0, strict=True)


class IntroOutroAsset(StrictModel):
    """One media-backed intro/outro placement with full provenance."""

    role: Literal["intro", "outro"]
    source_id: Identifier
    duration_frames: int = Field(gt=0, strict=True)
    asset: AssetProvenance


class PresentationSection(StrictModel):
    """The Phase-2 fixed presentation section carried inside a Resolve Package."""

    schema_version: Literal["phase-2-fixed-presentation-v1"]
    subtitle: FixedSubtitleStyle
    audio: FixedAudioPolicy
    render_preset: FixedRenderPreset
    intro_outro: tuple[IntroOutroAsset, ...] = Field(min_length=1)


__all__ = [
    "AssetProvenance",
    "AudioRole",
    "AudioRoleEntry",
    "FixedAudioPolicy",
    "FixedRenderPreset",
    "FixedSubtitleStyle",
    "IntroOutroAsset",
    "PresentationSection",
]
