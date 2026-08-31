"""Audio presentation models (Todo 59): the profile-driven audio section.

The compiled audio section carries the approved BGM/SE asset refs
(rights-gated upstream), their record-frame anchors with sample offsets,
millibel gains, and frame fades, the declared ducking (external mix only,
dialogue-triggered), the four logical tracks — dialogue / ambient / music /
sfx, never conflated onto one track — and the integer loudness / peak /
channel targets both the external mix normalization and the Todo-52 QC
checks bind to. All values are strict ints/strings and the section is
sealed over canonical bytes.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import (
    Identifier,
    RationalFrameRate,
    RecordFrameSpan,
    Sha256,
    StrictModel,
    to_tuple,
)
from services.contracts.serialization import GENESIS_SHA256, canonical_json_bytes

type Sequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(to_tuple)]

type LogicalRole = Literal["dialogue", "ambient", "music", "sfx"]
type AudioLadderRung = Literal[
    "external_mix_derivative", "verified_preset", "manual_fallback"
]
type AssetRefKind = Literal["tone", "se"]
type ProcessingKind = Literal["gain", "fades", "ducking", "loudness_normalization"]

LOGICAL_ROLES: tuple[LogicalRole, ...] = ("dialogue", "ambient", "music", "sfx")


class AudioAssetRef(StrictModel):
    """A rights-approved registry asset fixed by content hash."""

    kind: AssetRefKind
    asset_id: Identifier
    sha256: Sha256
    path: str = Field(min_length=1)


class AudioAnchor(StrictModel):
    """One BGM/SE placement: record span + sample offset + gain + fades."""

    item_id: Identifier
    role: Literal["music", "sfx"]
    asset_kind: AssetRefKind
    record_span: RecordFrameSpan
    offset_samples: int = Field(ge=0, strict=True)
    gain_mb: int = Field(ge=-12000, le=12000, strict=True)
    fade_in_frames: int = Field(ge=0, strict=True)
    fade_out_frames: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def require_fades_fit(self) -> AudioAnchor:
        if self.fade_in_frames + self.fade_out_frames >= max(
            1, self.record_span.length
        ):
            raise PydanticCustomError(
                "fade_span", "fades must be strictly shorter than the anchor"
            )
        return self


class DuckingDeclaration(StrictModel):
    """Ducking is declared intent, applied in the external mix only."""

    declared: bool
    trigger_role: Literal["dialogue"] = "dialogue"
    applied_in: Literal["external_mix"] = "external_mix"


class LogicalAudioTrack(StrictModel):
    """One logical role owning exactly one Resolve audio track."""

    role: LogicalRole
    resolve_track_index: int = Field(gt=0, strict=True)
    anchors: Sequence[AudioAnchor] = ()


class AudioTargets(StrictModel):
    """Integer loudness/peak/channel targets (mLUFS and mB encodings)."""

    loudness_target_mlufs: int = Field(lt=0, strict=True)
    loudness_tolerance_mlufs: int = Field(gt=0, le=3000, strict=True)
    max_peak_mb: int = Field(lt=0, strict=True)
    channels: int = Field(gt=0, le=8, strict=True)
    sample_rate_hz: int = Field(gt=0, strict=True)


class ProcessingAssignment(StrictModel):
    """Which role's processing is assigned to which role's track.

    An assignment whose ``role`` differs from ``on_track_role`` is the
    dialogue-processing-on-ambient conflation the pipeline refuses.
    """

    kind: ProcessingKind
    role: LogicalRole
    on_track_role: LogicalRole


class AudioProfileSection(StrictModel):
    """The compiled, hash-sealed audio plan for one episode."""

    schema_version: Literal["audio-profile-section-v1"] = (
        "audio-profile-section-v1"
    )
    episode_id: Identifier
    profile_snapshot_sha256: Sha256
    assets: dict[AssetRefKind, AudioAssetRef]
    logical_tracks: tuple[LogicalAudioTrack, ...] = Field(min_length=4)
    ducking: DuckingDeclaration
    assignments: tuple[ProcessingAssignment, ...] = Field(min_length=1)
    targets: AudioTargets
    total_frames: int = Field(gt=0, strict=True)
    frame_rate: RationalFrameRate
    section_sha256: Sha256

    @model_validator(mode="after")
    def require_role_separated_tracks(self) -> AudioProfileSection:
        roles = [track.role for track in self.logical_tracks]
        tracks = [track.resolve_track_index for track in self.logical_tracks]
        if set(roles) != set(LOGICAL_ROLES) or len(set(roles)) != len(roles):
            raise PydanticCustomError(
                "role_inventory", "logical tracks must be exactly the four roles"
            )
        if len(set(tracks)) != len(tracks):
            raise PydanticCustomError(
                "role_conflation", "each logical role owns a distinct track"
            )
        for track in self.logical_tracks:
            if track.role in ("dialogue", "ambient") and track.anchors:
                raise PydanticCustomError(
                    "anchor_role", "presentation anchors live on music/sfx only"
                )
            for anchor in track.anchors:
                if anchor.role != track.role:
                    raise PydanticCustomError(
                        "anchor_role", "anchor role must match its track"
                    )
                if anchor.record_span.end_frame > self.total_frames:
                    raise PydanticCustomError(
                        "anchor_span", "anchor exceeds the timeline extent"
                    )
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    def content_hash(self) -> Sha256:
        zeroed = self.model_copy(update={"section_sha256": GENESIS_SHA256})
        return hashlib.sha256(canonical_json_bytes(zeroed)).hexdigest()

    def verify_hash(self) -> bool:
        return self.section_sha256 == self.content_hash()


class AudioDerivativeRef(StrictModel):
    """The shared external mixed derivative, fixed by content hash."""

    path: str = Field(min_length=1)
    sha256: Sha256
    duration_samples: int = Field(gt=0, strict=True)
    sample_rate_hz: int = Field(gt=0, strict=True)
    channels: int = Field(gt=0, strict=True)


class AudioSection(StrictModel):
    """The package audio section: the chosen rung plus its hash bindings."""

    schema_version: Literal["audio-section-v1"] = "audio-section-v1"
    rung: AudioLadderRung
    reason: str = Field(min_length=1)
    profile_section_sha256: Sha256
    derivative: AudioDerivativeRef | None = None
    preset_name: str | None = None
    preset_probe_sha256: Sha256 | None = None
    logical_tracks: tuple[LogicalAudioTrack, ...] = Field(min_length=4)
    targets: AudioTargets
    total_frames: int = Field(gt=0, strict=True)
    frame_rate: RationalFrameRate

    @model_validator(mode="after")
    def require_role_separated_tracks(self) -> AudioSection:
        roles = [track.role for track in self.logical_tracks]
        tracks = [track.resolve_track_index for track in self.logical_tracks]
        if set(roles) != set(LOGICAL_ROLES) or len(set(roles)) != len(roles):
            raise PydanticCustomError(
                "role_inventory", "logical tracks must be exactly the four roles"
            )
        if len(set(tracks)) != len(tracks):
            raise PydanticCustomError(
                "role_conflation", "each logical role owns a distinct track"
            )
        return self

    @model_validator(mode="after")
    def require_rung_payload(self) -> AudioSection:
        if self.rung == "external_mix_derivative" and self.derivative is None:
            raise PydanticCustomError(
                "rung_payload", "the external rung requires the derivative ref"
            )
        if self.rung == "verified_preset" and (
            self.preset_name is None or self.preset_probe_sha256 is None
        ):
            raise PydanticCustomError(
                "rung_payload",
                "the preset rung requires a hash-bound preset binding",
            )
        if self.rung == "manual_fallback" and self.derivative is not None:
            raise PydanticCustomError(
                "rung_payload", "the manual rung carries no derivative"
            )
        return self


def clone_tracks(section: AudioProfileSection) -> tuple[LogicalAudioTrack, ...]:
    return tuple(
        LogicalAudioTrack(
            role=track.role,
            resolve_track_index=track.resolve_track_index,
            anchors=track.anchors,
        )
        for track in section.logical_tracks
    )


__all__ = [
    "LOGICAL_ROLES",
    "AssetRefKind",
    "AudioAnchor",
    "AudioAssetRef",
    "AudioDerivativeRef",
    "AudioLadderRung",
    "AudioProfileSection",
    "AudioSection",
    "AudioTargets",
    "DuckingDeclaration",
    "LogicalAudioTrack",
    "LogicalRole",
    "ProcessingAssignment",
    "ProcessingKind",
    "Sequence",
    "clone_tracks",
]
