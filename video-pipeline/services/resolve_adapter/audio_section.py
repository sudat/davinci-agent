"""The audio section of a Resolve Package (Todo 59).

The section is descriptive and hash-bound: the external-mix rung becomes
one additional ``AppendToTimeline`` media placement for the shared
derivative on the declared music track and one extra presented media
binding, while the base dialogue/ambient placements it REPLACES are
dropped (the derivative is the program audio). Role separation is
re-verified from the package's own inputs — dialogue/ambient conflation
is the typed ``audio-role-conflation`` package error before any build.
Packages compiled without an audio section are byte-identical to before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.presentation.audio_models import LOGICAL_ROLES
from services.resolve_adapter.errors import (
    AUDIO_ROLE_CONFLATION,
    WRONG_TRACK_MAP,
    PackageCompileError,
)
from services.resolve_adapter.models import (
    AppendPlacement,
    ClipInfo,
    MediaBinding,
)

if TYPE_CHECKING:
    from services.presentation.audio_models import AudioSection

AUDIO_SOURCE_ID: str = "audio.mix.derivative"


def _music_track(section: AudioSection) -> int:
    return next(
        track.resolve_track_index
        for track in section.logical_tracks
        if track.role == "music"
    )


def audio_media_bindings(section: AudioSection) -> tuple[MediaBinding, ...]:
    """The derivative as a presented media binding (external rung only)."""

    if section.derivative is None:
        return ()
    return (
        MediaBinding(
            source_id=AUDIO_SOURCE_ID,
            path=section.derivative.path,
            sha256=section.derivative.sha256,
            duration_frames=section.total_frames,
        ),
    )


def audio_placements(
    section: AudioSection, *, frame_origin: int
) -> tuple[AppendPlacement, ...]:
    """The derivative placement on the music track (external rung only)."""

    if section.derivative is None:
        return ()
    derivative = section.derivative
    expected_samples = section.total_frames * derivative.sample_rate_hz * (
        section.frame_rate.den
    ) // section.frame_rate.num
    if derivative.duration_samples != expected_samples:
        raise PackageCompileError(
            WRONG_TRACK_MAP,
            f"derivative carries {derivative.duration_samples} samples; the "
            f"timeline expects {expected_samples}",
        )
    return (
        AppendPlacement(
            item_id=AUDIO_SOURCE_ID,
            media_source_id=AUDIO_SOURCE_ID,
            capability="media_intro_outro",
            clip_info=ClipInfo(
                media_source_id=AUDIO_SOURCE_ID,
                start_frame=0,
                end_frame=section.total_frames,
                track_type="audio",
                track_index=_music_track(section),
                record_frame=frame_origin,
            ),
        ),
    )


def require_audio_role_separation(section: AudioSection) -> None:
    """Typed package gate: the four logical roles never share a track."""

    indices = [track.resolve_track_index for track in section.logical_tracks]
    roles = [track.role for track in section.logical_tracks]
    if set(roles) != set(LOGICAL_ROLES) or len(set(indices)) != len(indices):
        raise PackageCompileError(
            AUDIO_ROLE_CONFLATION,
            "dialogue/ambient/music/sfx must own four distinct audio tracks; "
            f"observed roles={roles} tracks={indices}",
        )


__all__ = [
    "AUDIO_SOURCE_ID",
    "audio_media_bindings",
    "audio_placements",
    "require_audio_role_separation",
]
