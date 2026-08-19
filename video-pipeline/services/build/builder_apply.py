"""Package application on the staging timeline: media, placement, links.

Placement uses only the verified AppendToTimeline clipInfo path — media
pool items with source span frames, an explicit Resolve track, and an
ABSOLUTE record frame at/above the timeline origin — appended one link
group at a time (the live-verified pair cadence). Post-placement
verification lives in :mod:`services.build.conformance`. There is
deliberately no code path that moves, trims, or otherwise structurally
mutates an already placed item: structural change means a fresh clean build.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.build.builder_models import BuildFailure
from services.resolve_adapter.models import AppendTrackMapEntry, RoleTrackMapEntry

if TYPE_CHECKING:
    from pathlib import Path

    from services.resolve_adapter.models import AppendPlacement, ResolvePackage
    from services.resolve_bridge.base_cut_models import (
        BaseCutMediaPoolItemApi,
        BaseCutTimelineItemApi,
    )
    from services.resolve_bridge.fixed_presentation_models import (
        FixedMediaPoolApi,
        FixedTimelineApi,
    )

MEDIA_TYPE: Final = {"video": 1, "audio": 2}
PLACEMENT_FIELDS: Final = 2


def import_media(
    pool: FixedMediaPoolApi, paths: dict[str, Path]
) -> dict[str, BaseCutMediaPoolItemApi]:
    ordered = sorted({str(path) for path in paths.values()})
    imported = pool.ImportMedia(ordered)
    if imported is None or len(imported) != len(ordered):
        raise BuildFailure(
            "media-import-failed",
            f"ImportMedia returned {len(imported or [])} items for {len(ordered)} files",
        )
    by_real: dict[str, BaseCutMediaPoolItemApi] = {}
    for item in imported:
        prop = item.GetClipProperty("File Path")
        if not isinstance(prop, str) or not prop:
            raise BuildFailure("media-import-failed", "imported media has no File Path property")
        by_real[os.path.realpath(prop)] = item
    media: dict[str, BaseCutMediaPoolItemApi] = {}
    for source_id, path in paths.items():
        item = by_real.get(os.path.realpath(str(path)))
        if item is None:
            raise BuildFailure("media-import-failed", f"imported media does not cover {path}")
        media[source_id] = item
    return media


def ensure_track_layout(timeline: FixedTimelineApi, package: ResolvePackage) -> None:
    for kind in ("video", "audio"):
        indexes = [
            entry.resolve_track_index
            for entry in package.track_map
            if isinstance(entry, AppendTrackMapEntry | RoleTrackMapEntry)
            and entry.resolve_track_type == kind
        ]
        want = max(indexes, default=1)
        while timeline.GetTrackCount(kind) < want:
            if kind == "audio":
                added = timeline.AddTrack(kind, "stereo")
            else:
                added = timeline.AddTrack(kind)
            if not added:
                raise BuildFailure("track-layout-failed", f"AddTrack failed: {kind}")
        while timeline.GetTrackCount(kind) > want:
            if not timeline.DeleteTrack(kind, timeline.GetTrackCount(kind)):
                raise BuildFailure("track-layout-failed", f"DeleteTrack failed: {kind}")


def _placement_groups(package: ResolvePackage) -> list[list[AppendPlacement]]:
    link_of = {
        item_id: group.av_link_id
        for group in package.link_groups
        for item_id in group.item_ids
    }
    grouped: dict[str, list[AppendPlacement]] = {}
    for placement in package.placements:
        key = link_of.get(placement.item_id, f"solo:{placement.item_id}")
        grouped.setdefault(key, []).append(placement)
    return sorted(
        grouped.values(),
        key=lambda group: min(p.clip_info.record_frame for p in group),
    )


def _clip_info(
    placement: AppendPlacement, media: dict[str, BaseCutMediaPoolItemApi]
) -> dict[str, object]:
    info = placement.clip_info
    return {
        "mediaPoolItem": media[info.media_source_id],
        "startFrame": info.start_frame,
        "endFrame": info.end_frame,
        "mediaType": MEDIA_TYPE[info.track_type],
        "trackIndex": info.track_index,
        "recordFrame": info.record_frame,
    }


def place_items(
    pool: FixedMediaPoolApi,
    package: ResolvePackage,
    media: dict[str, BaseCutMediaPoolItemApi],
) -> dict[str, BaseCutTimelineItemApi]:
    handles: dict[str, BaseCutTimelineItemApi] = {}
    for group in _placement_groups(package):
        infos = [_clip_info(placement, media) for placement in group]
        added = pool.AppendToTimeline(infos)
        if added is None or len(added) != len(infos):
            ids = ",".join(placement.item_id for placement in group)
            raise BuildFailure(
                "placement-failed",
                f"AppendToTimeline returned {len(added or [])} items for [{ids}]",
            )
        for placement, item in zip(group, added, strict=True):
            handles[placement.item_id] = item
    return handles


def apply_link_groups(
    timeline: FixedTimelineApi,
    package: ResolvePackage,
    handles: dict[str, BaseCutTimelineItemApi],
) -> None:
    for group in package.link_groups:
        members = [handles[item_id] for item_id in group.item_ids]
        if not timeline.SetClipsLinked(members, True):  # noqa: FBT003 (Resolve API flag)
            raise BuildFailure("link-failed", f"SetClipsLinked failed for {group.av_link_id}")


__all__ = [
    "apply_link_groups",
    "ensure_track_layout",
    "import_media",
    "place_items",
]
