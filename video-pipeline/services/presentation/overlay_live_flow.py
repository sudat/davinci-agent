"""The Todo-58 live overlay flow: owned build, dual path, pixel verification.

Orchestrates :mod:`services.presentation.overlay_live_ops` inside one owned
``__fvp_test__`` project: render the external transparent overlay, place
the base cut + overlay, verify typed structural readback, run the guarded
Fusion apply when (and only when) the section decided the verified fusion
path, render, and verify rendered presence from pixels. The final report
payload is assembled here and written by the CLI driver.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from services.foundation_io import atomic_write
from services.presentation.overlay_live_fusion import apply_fusion_guarded
from services.presentation.overlay_live_media import (
    BASE_SPAN,
    FRAME_ORIGIN,
    GEO,
    MARKER,
    OVERLAY_SPAN,
    RATE_NUM,
    REPORT_NAME,
    TIMELINE_START_TC,
    LivePoolApi,
    LiveTimelineApi,
    OverlayLiveError,
    _append,
    _ensure_tracks,
    _import,
    _readback,
    _render_project,
)
from services.presentation.overlay_live_report import _media_row, _report_payload
from services.presentation.overlay_paths import (
    build_overlay_section,
    require_pixel_evidence,
    verify_placement_readback,
)
from services.presentation.overlay_render import (
    render_transparent_overlay,
    verify_rendered_presence,
)
from services.resolve_bridge.lifecycle import (
    create_disposable_project,
    create_owned_timeline,
    owned_project_name,
    owned_timeline_name,
)

if TYPE_CHECKING:
    from services.presentation.overlay_models import (
        FusionSupport,
        OverlayItemRequest,
        OverlayPathDecision,
        OverlayPlacementReadback,
        OverlaySection,
    )
    from services.resolve_bridge.connection import ResolveConnection
    from services.resolve_bridge.fixed_presentation_models import FixedProjectApi



class OverlayFlow:
    """One live overlay build; every outcome is recorded honestly."""

    def __init__(
        self,
        *,
        connection: ResolveConnection,
        bundle: Path,
        host_report: Path,
        ffmpeg: Path,
        ffprobe: Path,
        base_source: Path,
        overlay_asset: Path,
        overlay_asset_sha: str,
        support: FusionSupport,
        item_requests: tuple[OverlayItemRequest, ...],
        render_deadline: float,
    ) -> None:
        self.connection = connection
        self.bundle = bundle
        self.host_report = host_report
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.base_source = base_source
        self.overlay_asset = overlay_asset
        self.overlay_asset_sha = overlay_asset_sha
        self.support = support
        self.item_requests = item_requests
        self.render_deadline = render_deadline
        self.fusion_status = "not_requested"
        self.passed = False

    @property
    def fusion_label(self) -> str:
        if self.fusion_status == "verified":
            return "fusion_applied"
        if self.fusion_status in {"fallback", "timeout"}:
            return "explicit_fallback"
        return "not_requested"

    def run(self) -> None:
        external = self.item_requests[0]
        rendered = render_transparent_overlay(
            ffmpeg_bin=self.ffmpeg,
            ffprobe_bin=self.ffprobe,
            asset=self.overlay_asset,
            asset_sha256=self.overlay_asset_sha,
            region=GEO.region_for(external.anchor),
            timeline_width=GEO.timeline_width,
            timeline_height=GEO.timeline_height,
            rate_num=RATE_NUM,
            duration_frames=external.record_span.end_frame
            - external.record_span.start_frame,
            output=self.bundle / "overlay-media.mov",
        )
        section = build_overlay_section(
            self.item_requests,
            self.support,
            GEO,
            rendered={
                external.item_id: _media_row(external.item_id, rendered)
            },
            overlay_track_index=2,
        )
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
                raise OverlayLiveError(f"SetSetting({key}) failed")
        timeline_name = owned_timeline_name()
        timeline = cast(
            "LiveTimelineApi", create_owned_timeline(project_api, timeline_name)
        )
        if not timeline.SetStartTimecode(TIMELINE_START_TC):
            raise OverlayLiveError("SetStartTimecode failed")
        _ensure_tracks(timeline)
        pool = project.GetMediaPool()
        if pool is None:
            raise OverlayLiveError("media pool unavailable")
        live_pool = cast("LivePoolApi", pool)
        media = _import(live_pool, [str(self.base_source), str(rendered.path)])
        self._append_base(live_pool, media[str(self.base_source)])
        overlay_decision = section.decisions[0]
        readback = self._append_overlay(
            live_pool, media[str(rendered.path)], overlay_decision
        )
        fusion_report = self._fusion_step(section, project_api.GetName(), timeline_name)
        render_path = _render_project(
            project, self.bundle / "render", self.render_deadline
        )
        checks = verify_rendered_presence(
            ffmpeg_bin=self.ffmpeg,
            render_path=render_path,
            width=GEO.timeline_width,
            height=GEO.timeline_height,
            decision=overlay_decision,
            anchors=section.anchors,
            expected_rgb=rendered.expected_rgb,
        )
        require_pixel_evidence(
            overlay_decision.item_id,
            pixels_verified=all(check.passed for check in checks),
            frames_extracted=len(checks),
        )
        self.fusion_status = str(fusion_report.get("status", "not_requested"))
        self.passed = True
        payload = _report_payload(
            section=section,
            rendered=rendered,
            readback=readback,
            fusion=fusion_report,
            checks=checks,
            render_path=render_path,
        )
        atomic_write(
            self.bundle / REPORT_NAME,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        )
        print(
            f"{MARKER} path=external_media track=2 "
            f"span={OVERLAY_SPAN[0]}..{OVERLAY_SPAN[1]} "
            f"rendered-presence=verified frames={len(checks)} coverage="
            f"{next(c.coverage_percent for c in checks if c.anchor.expect_covered):.1f}%"
        )
        if self.fusion_status == "verified":
            controls = fusion_report.get("controls")
            count = len(controls) if isinstance(controls, list) else 0
            print(f"{MARKER} fusion-controls-verified={count}/3")

    def _append_base(self, pool: LivePoolApi, base_item: object) -> None:
        for media_type, track_index in ((1, 1), (2, 1)):
            _append(
                pool,
                {
                    "mediaPoolItem": base_item,
                    "startFrame": BASE_SPAN[0],
                    "endFrame": BASE_SPAN[1],
                    "mediaType": media_type,
                    "trackIndex": track_index,
                    "recordFrame": FRAME_ORIGIN,
                },
            )

    def _append_overlay(
        self,
        pool: object,
        overlay_item_media: object,
        decision: OverlayPathDecision,
    ) -> OverlayPlacementReadback:
        placed = _append(
            cast("LivePoolApi", pool),
            {
                "mediaPoolItem": overlay_item_media,
                "startFrame": 0,
                "endFrame": OVERLAY_SPAN[1] - OVERLAY_SPAN[0],
                "mediaType": 1,
                "trackIndex": 2,
                "recordFrame": FRAME_ORIGIN + OVERLAY_SPAN[0],
            },
        )
        readback = _readback(decision.item_id, placed)
        verify_placement_readback(
            decision, readback, track_index=2, frame_origin=FRAME_ORIGIN
        )
        return readback

    def _fusion_step(
        self, section: OverlaySection, project_name: str, timeline_name: str
    ) -> dict[str, object]:
        fusion_decision = section.decisions[1]
        if fusion_decision.path == "fusion_template":
            return apply_fusion_guarded(
                host_report=self.host_report,
                bundle=self.bundle,
                project_name=project_name,
                timeline_name=timeline_name,
            )
        if fusion_decision.reason in {
            "fusion_unsupported_explicit_fallback",
            "unsupported_control_explicit_fallback",
        }:
            return {
                "status": "fallback",
                "reason": fusion_decision.reason,
                "item_id": fusion_decision.item_id,
            }
        return {"status": "not_requested"}
