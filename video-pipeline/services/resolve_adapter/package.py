"""Compile the validated production Timeline IR into a Resolve Package.

A deterministic NLE-instruction manifest (NOT API calls): AppendToTimeline
clipInfo placements in the timeline-absolute frame space (origin
01:00:00:00 => frame 108000), the fixed subtitle as an EXTERNAL post-render
mov_text step anchored to record frames, the audio preset in the render
settings, and a render job bound to CompletionPercentage==100 +
SelectAllFrames + the fixed preset. All inputs pass
:mod:`services.resolve_adapter.validate` first; same IR + lock + matrix =>
byte-identical canonical bytes with the content hash chained over the
payload.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from services.contracts.primitives import ArtifactRef, Producer
from services.contracts.timeline_ir import (
    SubtitleCueItem,
    TimelineGapItem,
    TimelineIrProduction,
    TimelineItem0C,
)
from services.foundation_io import canonical_model_bytes
from services.resolve_adapter.audio_section import (
    audio_placements,
    require_audio_role_separation,
)
from services.resolve_adapter.errors import WRONG_TRACK_MAP, PackageCompileError
from services.resolve_adapter.models import (
    AppendPlacement,
    AppendTrackMapEntry,
    ClipInfo,
    ExternalTrackMapEntry,
    InputsView,
    LinkGroup,
    MediaBinding,
    RenderJobSpec,
    ResolvePackage,
    TimelineView,
    TrackMapEntry,
)
from services.resolve_adapter.overlay_section import (
    overlay_placements,
)
from services.resolve_adapter.presentation_baseline import (
    AppliedPresentation,
    apply_presentation,
    subtitle_step,
)
from services.resolve_adapter.presentation_styled import attach_styled_presentation
from services.resolve_adapter.validate import verify_compile_inputs

if TYPE_CHECKING:
    from services.presentation.audio_models import AudioSection
    from services.presentation.color_models import ColorSection
    from services.presentation.overlay_models import OverlaySection
    from services.presentation.styling_models import StyledPresentation
    from services.resolve_adapter.presentation_models import PresentationSection
    from services.toolchain.models import Phase2ToolchainLock

PRODUCER: Final = Producer(name="resolve-adapter", version="1")
LINK_GROUP_MIN_MEMBERS: Final = 2
FRAME_ORIGIN: Final = 108000
START_TIMECODE: Final = "01:00:00:00"
ZERO_HASH: Final = "0" * 64


@dataclass(frozen=True, slots=True)
class PackageCompileRequest:
    ir: TimelineIrProduction
    lock: Phase2ToolchainLock
    lock_sha256: str
    declared_media: tuple[MediaBinding, ...]
    artifact_id: str
    presented_media: tuple[MediaBinding, ...] | None = None
    intro_outro_source_ids: frozenset[str] = frozenset()
    presentation: PresentationSection | None = None
    styled_presentation: StyledPresentation | None = None
    overlay_paths: OverlaySection | None = None
    audio_section: AudioSection | None = None
    color_section: ColorSection | None = None

    def resolved_presented_media(self) -> tuple[MediaBinding, ...]:
        return self.declared_media if self.presented_media is None else self.presented_media


def _placements(
    request: PackageCompileRequest, applied: AppliedPresentation | None
) -> tuple[AppendPlacement, ...]:
    ordered: list[AppendPlacement] = []
    for track_type in ("video", "audio"):
        for track in request.ir.tracks:
            if track.track.kind != track_type:
                continue
            for item in track.items:
                if isinstance(item, TimelineGapItem | SubtitleCueItem):
                    continue
                track_index = 1
                if track_type == "audio" and applied is not None:
                    track_index = applied.audio_track_of(item.source.source_id)
                ordered.append(
                    AppendPlacement(
                        item_id=item.item_id,
                        media_source_id=item.source.source_id,
                        capability=(
                            "media_intro_outro"
                            if item.source.source_id in request.intro_outro_source_ids
                            else "base_cut"
                        ),
                        clip_info=ClipInfo(
                            media_source_id=item.source.source_id,
                            start_frame=item.source.span.start_frame,
                            end_frame=item.source.span.end_frame,
                            track_type=track_type,
                            track_index=track_index,
                            record_frame=FRAME_ORIGIN + item.record_span.start_frame,
                        ),
                    )
                )
    return tuple(ordered)


def _link_groups(
    placements: tuple[AppendPlacement, ...], ir: TimelineIrProduction
) -> tuple[LinkGroup, ...]:
    link_of: dict[str, str] = {}
    for track in ir.tracks:
        for item in track.items:
            if isinstance(item, TimelineItem0C) and item.av_link_id is not None:
                link_of[item.item_id] = item.av_link_id
    members: dict[str, list[str]] = {}
    for placement in placements:
        link_id = link_of.get(placement.item_id)
        if link_id is not None:
            members.setdefault(link_id, []).append(placement.item_id)
    return tuple(
        LinkGroup(av_link_id=link_id, item_ids=tuple(sorted(ids)))
        for link_id, ids in members.items()
        if len(ids) >= LINK_GROUP_MIN_MEMBERS
    )


def _extent_frames(ir: TimelineIrProduction) -> int:
    extent = 0
    for track in ir.tracks:
        if track.track.kind == "subtitle":
            continue
        for item in track.items:
            extent = max(extent, item.record_span.end_frame)
    if extent <= 0:
        raise PackageCompileError(WRONG_TRACK_MAP, "IR has no A/V record extent")
    return extent


def compile_resolve_package(request: PackageCompileRequest) -> ResolvePackage:
    verify_compile_inputs(
        request.ir,
        request.lock,
        request.declared_media,
        request.resolved_presented_media(),
    )
    preset = request.lock.render_qc.preset
    applied = (
        apply_presentation(
            request.presentation,
            request.ir,
            request.lock,
            request.declared_media,
            request.intro_outro_source_ids,
        )
        if request.presentation is not None
        else None
    )
    placements = _placements(request, applied)
    replaces_audio = (
        request.audio_section is not None
        and request.audio_section.rung == "external_mix_derivative"
    )
    if replaces_audio:
        placements = tuple(
            placement for placement in placements
            if placement.clip_info.track_type != "audio"
        )
    if request.overlay_paths is not None:
        overlay_extra = overlay_placements(
            request.overlay_paths, frame_origin=FRAME_ORIGIN
        )
        presented_ids = {
            binding.source_id
            for binding in request.resolved_presented_media()
        }
        missing = [
            placement.clip_info.media_source_id
            for placement in overlay_extra
            if placement.clip_info.media_source_id not in presented_ids
        ]
        if missing:
            raise PackageCompileError(
                WRONG_TRACK_MAP,
                f"overlay placements have no presented media binding: {missing}",
            )
        placements = (*placements, *overlay_extra)
    if replaces_audio and request.audio_section is not None:
        require_audio_role_separation(request.audio_section)
        audio_extra = audio_placements(
            request.audio_section, frame_origin=FRAME_ORIGIN
        )
        presented_ids = {
            binding.source_id
            for binding in request.resolved_presented_media()
        }
        missing_audio = [
            placement.clip_info.media_source_id
            for placement in audio_extra
            if placement.clip_info.media_source_id not in presented_ids
        ]
        if missing_audio:
            raise PackageCompileError(
                WRONG_TRACK_MAP,
                f"audio derivative has no presented media binding: {missing_audio}",
            )
        placements = (*placements, *audio_extra)
    track_map: tuple[TrackMapEntry, ...] = (
        AppendTrackMapEntry(
            logical_kind="video",
            logical_index=1,
            resolve_track_type="video",
            resolve_track_index=1,
        ),
        AppendTrackMapEntry(
            logical_kind="audio",
            logical_index=2,
            resolve_track_type="audio",
            resolve_track_index=1,
        ),
        ExternalTrackMapEntry(logical_kind="subtitle", logical_index=3),
    )
    if applied is not None and not replaces_audio:
        track_map = (
            track_map[0],
            *applied.audio_tracks,
            track_map[2],
        )
    if replaces_audio and request.audio_section is not None:
        music_index = next(
            track.resolve_track_index
            for track in request.audio_section.logical_tracks
            if track.role == "music"
        )
        track_map = (
            track_map[0],
            AppendTrackMapEntry(
                logical_kind="audio",
                logical_index=2,
                resolve_track_type="audio",
                resolve_track_index=music_index,
            ),
            track_map[-1],
        )
    package = ResolvePackage(
        artifact_id=request.artifact_id,
        artifact_type="resolve_package_v1",
        schema_version="resolve-package-v1",
        content_hash=ZERO_HASH,
        producer=PRODUCER,
        inputs=(
            ArtifactRef(
                artifact_id=request.ir.artifact_id,
                sha256=hashlib.sha256(canonical_model_bytes(request.ir)).hexdigest(),
            ),
        ),
        timeline=TimelineView(
            frame_rate=request.ir.rate,
            width=preset.width,
            height=preset.height,
            audio_sample_rate=preset.audio_sample_rate,
            start_timecode=START_TIMECODE,
            frame_origin=FRAME_ORIGIN,
        ),
        track_map=track_map,
        placements=placements,
        link_groups=_link_groups(placements, request.ir),
        subtitle_step=subtitle_step(request.ir),
        render_job=RenderJobSpec(
            video_format=preset.video_format,
            video_codec=preset.video_codec,
            width=preset.width,
            height=preset.height,
            frame_rate=request.ir.rate,
            audio_codec=preset.audio_codec,
            audio_sample_rate=preset.audio_sample_rate,
            audio_channels=preset.audio_channels,
            timeline_start_timecode=START_TIMECODE,
            frame_origin=FRAME_ORIGIN,
            extent_frames=_extent_frames(request.ir),
        ),
        inputs_view=InputsView(
            capability_matrix_path=request.lock.resolve_package.capability_matrix_path,
            capability_matrix_sha256=request.lock.resolve_package.capability_matrix_sha256,
            toolchain_lock_sha256=request.lock_sha256,
            declared_media=request.declared_media,
        ),
        presentation=request.presentation,
        styled_presentation=(
            attach_styled_presentation(request.styled_presentation, request.ir)
            if request.styled_presentation is not None
            else None
        ),
        overlay_paths=request.overlay_paths,
        audio=request.audio_section,
        color=request.color_section,
    )
    digest = hashlib.sha256(canonical_model_bytes(package)).hexdigest()
    return package.model_copy(update={"content_hash": digest})


__all__ = ["PackageCompileRequest", "compile_resolve_package"]
