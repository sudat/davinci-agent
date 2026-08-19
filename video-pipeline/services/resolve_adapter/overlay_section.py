"""The overlay section of a Resolve Package (Todo 58).

The section is descriptive and hash-bound: every external-path decision
becomes an additional ``AppendToTimeline`` media placement on the declared
top video track (the established, capability-verified media path), and the
rendered overlay media becomes an extra presented media binding. Fusion
decisions are carried as decisions only — their guarded application is the
builder's overlay step, never a media placement. Nothing here mutates the
frozen phase-2 surface: packages compiled without an overlay section are
byte-identical to before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.presentation.overlay_paths import DEFAULT_OVERLAY_TRACK_INDEX
from services.resolve_adapter.models import (
    AppendPlacement,
    ClipInfo,
    MediaBinding,
)

if TYPE_CHECKING:
    from services.presentation.overlay_models import OverlaySection

OVERLAY_SOURCE_PREFIX: str = "overlay."


def overlay_media_bindings(section: OverlaySection) -> tuple[MediaBinding, ...]:
    """One presented media binding per rendered external overlay."""

    return tuple(
        MediaBinding(
            source_id=f"{OVERLAY_SOURCE_PREFIX}{row.item_id}",
            path=row.path,
            sha256=row.sha256,
            duration_frames=row.duration_frames,
        )
        for row in section.rendered
    )


def overlay_placements(
    section: OverlaySection, *, frame_origin: int
) -> tuple[AppendPlacement, ...]:
    """External-path decisions as verified media placements on the top track."""

    placements: list[AppendPlacement] = []
    for decision in section.decisions:
        if decision.path != "external_media":
            continue
        duration = decision.record_span.end_frame - decision.record_span.start_frame
        placements.append(
            AppendPlacement(
                item_id=decision.item_id,
                media_source_id=f"{OVERLAY_SOURCE_PREFIX}{decision.item_id}",
                capability="media_intro_outro",
                clip_info=ClipInfo(
                    media_source_id=f"{OVERLAY_SOURCE_PREFIX}{decision.item_id}",
                    start_frame=0,
                    end_frame=duration,
                    track_type="video",
                    track_index=section.overlay_track_index,
                    record_frame=frame_origin + decision.record_span.start_frame,
                ),
            )
        )
    return tuple(placements)


def require_overlay_track_free(
    overlay_track_index: int, *, base_video_tracks: int
) -> None:
    """The overlay track must sit above every base video track."""

    if overlay_track_index <= base_video_tracks:
        raise ValueError(
            f"overlay video track {overlay_track_index} must be above the base "
            f"video tracks ({base_video_tracks})"
        )


__all__ = [
    "DEFAULT_OVERLAY_TRACK_INDEX",
    "OVERLAY_SOURCE_PREFIX",
    "overlay_media_bindings",
    "overlay_placements",
    "require_overlay_track_free",
]
