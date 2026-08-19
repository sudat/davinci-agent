"""The Phase-2 FIXED presentation baseline: build, apply, and the subtitle step.

This module owns everything the frozen Phase-2 presentation section is
allowed to be: the fixed external-rung subtitle step (mov_text, record-frame
anchors), the dialogue/ambient role table routed onto role-separated Resolve
audio tracks, the 0A-verified render contract, and media-backed intro/outro
assets with mandatory {path, sha256, license_ref} provenance. Phase-3
config is refused by :mod:`services.resolve_adapter.presentation_scope`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from services.contracts.timeline_ir import SubtitleCueItem, TimelineItem0C
from services.resolve_adapter.errors import (
    AUDIO_ROLE_MISSING,
    CUE_TIMING_INEXACT,
    MEDIA_BINDING_MISSING,
    PHASE3_SCOPE,
    PRESENTATION_INVALID,
    RENDER_PRESET_MISMATCH,
    SUBTITLE_UNSAFE_AREA,
    UNAPPROVED_ASSET,
    PackageCompileError,
)
from services.resolve_adapter.models import (
    SUBTITLE_MUX_ARGV,
    RoleTrackMapEntry,
    SubtitleCueInstruction,
    SubtitlePostRenderStep,
)
from services.resolve_adapter.presentation_models import PresentationSection

if TYPE_CHECKING:
    from services.contracts.timeline_ir import TimelineIrProduction
    from services.resolve_adapter.models import MediaBinding
    from services.resolve_adapter.presentation_models import (
        FixedAudioPolicy,
        FixedRenderPreset,
        FixedSubtitleStyle,
        IntroOutroAsset,
    )
    from services.toolchain.models import Phase2ToolchainLock


def _ms(frames: int, num: int, den: int) -> int:
    return (frames * 1000 * den + num // 2) // num


def subtitle_step(ir: TimelineIrProduction) -> SubtitlePostRenderStep | None:
    """The fixed external subtitle step anchored to record frames (Todo 17)."""

    cues: list[SubtitleCueInstruction] = []
    for track in ir.tracks:
        if track.track.kind != "subtitle":
            continue
        for item in track.items:
            if not isinstance(item, SubtitleCueItem):
                continue
            start_ms = _ms(item.record_span.start_frame, ir.rate.num, ir.rate.den)
            end_ms = _ms(item.record_span.end_frame, ir.rate.num, ir.rate.den)
            if end_ms <= start_ms:
                raise PackageCompileError(
                    CUE_TIMING_INEXACT,
                    f"{item.item_id}: millisecond rounding collapsed the cue span",
                )
            cues.append(
                SubtitleCueInstruction(
                    cue_id=item.item_id,
                    text=item.text,
                    lines=item.lines,
                    style_ref=item.style_ref,
                    min_duration_frames=item.min_duration_frames,
                    anchor_record_start_frame=item.record_span.start_frame,
                    anchor_record_end_frame=item.record_span.end_frame,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            )
    if not cues:
        return None
    return SubtitlePostRenderStep(argv=SUBTITLE_MUX_ARGV, cues=tuple(cues))


def _audio_sources(ir: TimelineIrProduction) -> frozenset[str]:
    sources: set[str] = set()
    for track in ir.tracks:
        if track.track.kind != "audio":
            continue
        for item in track.items:
            if isinstance(item, TimelineItem0C):
                sources.add(item.source.source_id)
    return frozenset(sources)


def _verify_ir_agreement(section: PresentationSection, ir: TimelineIrProduction) -> None:
    roles = {entry.source_id: entry.role for entry in section.audio.roles}
    for source_id in sorted(_audio_sources(ir) ^ frozenset(roles)):
        raise PackageCompileError(
            AUDIO_ROLE_MISSING,
            f"{source_id}: every audio source carries exactly one dialogue/ambient role",
        )
    for track in ir.tracks:
        if track.track.kind != "subtitle":
            continue
        for item in track.items:
            if not isinstance(item, SubtitleCueItem):
                continue
            if not item.safe_area:
                raise PackageCompileError(
                    SUBTITLE_UNSAFE_AREA,
                    f"{item.item_id}: cue is not safe-area compliant (Todo-44 QC)",
                )
            if item.style_ref != section.subtitle.style_ref:
                raise PackageCompileError(
                    PHASE3_SCOPE,
                    f"{item.item_id}: style_ref {item.style_ref} != fixed "
                    f"{section.subtitle.style_ref}; style swapping is Phase-3 config",
                )


@dataclass(frozen=True, slots=True)
class PresentationBuildRequest:
    ir: TimelineIrProduction
    subtitle: FixedSubtitleStyle
    audio: FixedAudioPolicy
    render_preset: FixedRenderPreset
    intro_outro: tuple[IntroOutroAsset, ...]


def build_presentation_section(request: PresentationBuildRequest) -> PresentationSection:
    """Emit the fixed presentation section from the IR + analysis artifacts."""

    section = PresentationSection(
        schema_version="phase-2-fixed-presentation-v1",
        subtitle=request.subtitle,
        audio=request.audio,
        render_preset=request.render_preset,
        intro_outro=request.intro_outro,
    )
    _verify_ir_agreement(section, request.ir)
    return section


@dataclass(frozen=True, slots=True)
class AppliedPresentation:
    section: PresentationSection
    audio_track_of: Callable[[str], int]
    audio_tracks: tuple[RoleTrackMapEntry, ...]


def apply_presentation(
    section: PresentationSection,
    ir: TimelineIrProduction,
    lock: Phase2ToolchainLock,
    declared_media: tuple[MediaBinding, ...],
    intro_outro_source_ids: frozenset[str],
) -> AppliedPresentation:
    """Validate the section against the lock/media and derive role routing."""

    _verify_ir_agreement(section, ir)
    preset = lock.render_qc.preset
    contract = section.render_preset
    for field, want, got in (
        ("audio_codec", preset.audio_codec, contract.audio_codec),
        ("audio_sample_rate", preset.audio_sample_rate, contract.audio_sample_rate),
        ("audio_channels", preset.audio_channels, contract.audio_channels),
    ):
        if want != got:
            raise PackageCompileError(
                RENDER_PRESET_MISMATCH, f"{field}: presentation {got} != locked {want}"
            )
    declared = {binding.source_id: binding for binding in declared_media}
    if {asset.source_id for asset in section.intro_outro} != intro_outro_source_ids:
        raise PackageCompileError(
            PRESENTATION_INVALID,
            "intro/outro provenance must cover exactly the intro_outro_source_ids",
        )
    for asset in section.intro_outro:
        binding = declared.get(asset.source_id)
        if binding is None:
            raise PackageCompileError(
                MEDIA_BINDING_MISSING, f"no media binding for intro/outro {asset.source_id}"
            )
        if (
            binding.sha256 != asset.asset.sha256
            or binding.duration_frames != asset.duration_frames
        ):
            raise PackageCompileError(
                UNAPPROVED_ASSET,
                f"{asset.source_id}: provenance sha256/duration disagrees with declared media",
            )
    roles = {entry.source_id: entry.role for entry in section.audio.roles}
    tracks = {
        "dialogue": section.audio.dialogue_resolve_track,
        "ambient": section.audio.ambient_resolve_track,
    }
    used = {roles[source_id] for source_id in _audio_sources(ir)}
    entries = tuple(
        RoleTrackMapEntry(role=role, resolve_track_index=tracks[role])
        for role in ("dialogue", "ambient")
        if role in used
    )
    return AppliedPresentation(
        section=section,
        audio_track_of=lambda source_id: tracks[roles[source_id]],
        audio_tracks=entries,
    )


__all__ = [
    "AppliedPresentation",
    "PresentationBuildRequest",
    "apply_presentation",
    "build_presentation_section",
    "subtitle_step",
]
