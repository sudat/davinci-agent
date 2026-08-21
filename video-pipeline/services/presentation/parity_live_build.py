"""Live parity timeline construction (Todo 61): one build, shared derivatives.

The deterministic bed wav plus the owned ``__fvp_test__`` timeline both
renders come from: declared-recipe bars base on V1, the SHARED transparent
overlay derivative on V2, and the SHARED external mixed audio derivative
on A1 — every placement read back structurally before any render.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from services.presentation.audio_live_media import (
    FRAME_ORIGIN,
    TIMELINE_START_TC,
    AudioPoolApi,
    append_clip,
    import_media,
    verify_audio_readback,
)
from services.presentation.overlay_live_media import GEO, OVERLAY_SPAN, LiveTimelineApi
from services.presentation.parity_live_media import ParityLiveError
from services.resolve_bridge.lifecycle import (
    create_disposable_project,
    create_owned_timeline,
    owned_project_name,
    owned_timeline_name,
)

if TYPE_CHECKING:
    from services.resolve_bridge.connection import ProjectManagerApi
    from services.resolve_bridge.fixed_presentation_models import FixedProjectApi


class BuilderConnection(Protocol):
    """The build surface the parity builder needs from a connection."""

    def project_manager(self) -> ProjectManagerApi: ...


RATE_NUM = 30
TOTAL_FRAMES = 600
OVERLAY_TRACK_INDEX = 2
BED_TONE_HZ = 220


class ParityTimelineBuilder:
    """Build the single timeline the preview and final renders share."""

    def __init__(self, *, connection: BuilderConnection, ffmpeg: Path) -> None:
        self.connection = connection
        self.ffmpeg = ffmpeg

    def bed_wav(self, out: Path) -> Path:
        argv = (
            str(self.ffmpeg),
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            (
                f"aevalsrc=exprs=0.2*sin(2*PI*{BED_TONE_HZ}*t)"
                f":s=48000:d={TOTAL_FRAMES // RATE_NUM}"
            ),
            "-c:a",
            "pcm_s16le",
            "-map_metadata",
            "-1",
            str(out),
        )
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0 or not out.is_file():
            raise ParityLiveError("bed_failed", result.stderr[-300:])
        return out

    def build(
        self, bars: Path, mix_path: Path, overlay_path: Path
    ) -> FixedProjectApi:
        project_api = create_disposable_project(
            self.connection.project_manager(), owned_project_name()
        )
        project = cast("FixedProjectApi", project_api)
        for key, value in (
            ("timelineFrameRate", str(RATE_NUM)),
            ("timelineResolutionWidth", str(GEO.timeline_width)),
            ("timelineResolutionHeight", str(GEO.timeline_height)),
        ):
            if not project.SetSetting(key, value):
                raise ParityLiveError("setup_failed", f"SetSetting({key}) failed")
        timeline = cast(
            "LiveTimelineApi",
            create_owned_timeline(project_api, owned_timeline_name()),
        )
        if not timeline.SetStartTimecode(TIMELINE_START_TC):
            raise ParityLiveError("setup_failed", "SetStartTimecode failed")
        track_wanted = timeline.GetTrackCount("video") < OVERLAY_TRACK_INDEX
        if track_wanted and not timeline.AddTrack("video"):
            raise ParityLiveError("setup_failed", "second video track unavailable")
        pool = project.GetMediaPool()
        if pool is None:
            raise ParityLiveError("setup_failed", "media pool unavailable")
        live_pool = cast("AudioPoolApi", pool)
        media = import_media(live_pool, [str(bars), str(mix_path), str(overlay_path)])
        video = append_clip(
            live_pool,
            {
                "mediaPoolItem": media[str(bars)],
                "startFrame": 0,
                "endFrame": TOTAL_FRAMES,
                "mediaType": 1,
                "trackIndex": 1,
                "recordFrame": FRAME_ORIGIN,
            },
        )
        if video.GetTrackTypeAndIndex()[0] != "video":
            raise ParityLiveError("setup_failed", "base video placed on wrong track")
        overlay_clip = append_clip(
            live_pool,
            {
                "mediaPoolItem": media[str(overlay_path)],
                "startFrame": 0,
                "endFrame": OVERLAY_SPAN[1] - OVERLAY_SPAN[0],
                "mediaType": 1,
                "trackIndex": OVERLAY_TRACK_INDEX,
                "recordFrame": FRAME_ORIGIN + OVERLAY_SPAN[0],
            },
        )
        if overlay_clip.GetTrackTypeAndIndex() != ["video", OVERLAY_TRACK_INDEX]:
            raise ParityLiveError("setup_failed", "overlay placed on wrong track")
        audio_item = append_clip(
            live_pool,
            {
                "mediaPoolItem": media[str(mix_path)],
                "startFrame": 0,
                "endFrame": TOTAL_FRAMES,
                "mediaType": 2,
                "trackIndex": 1,
                "recordFrame": FRAME_ORIGIN,
            },
        )
        verify_audio_readback(
            audio_item,
            track_index=1,
            start_frame=FRAME_ORIGIN,
            end_frame=FRAME_ORIGIN + TOTAL_FRAMES,
        )
        return project


__all__ = ["ParityTimelineBuilder"]
