"""The dual-path overlay applier (Todo 58): choose, verify, never fake.

The chooser honors the probe-verified Fusion capability with explicit
recorded fallbacks; the readback and pixel-evidence gates make API-only
success impossible: an overlay is applied only when its final rendered
presence, position, and duration are verified from extracted frame pixels.
"""

from __future__ import annotations

from pathlib import Path

from services.presentation.overlay_models import (
    PUBLISHED_CONTROLS,
    FusionSupport,
    OverlayGeometry,
    OverlayItemRequest,
    OverlayPathDecision,
    OverlayPathError,
    OverlayPlacementReadback,
    OverlayRenderedMedia,
    OverlaySection,
    PresenceAnchor,
)
from services.spike.gate_models import CapabilityMatrix

PLACEMENT_FINDING: str = "fusion-title-template-placement"
CONTROL_FINDING: str = "fusion-title-control-readback"
DEFAULT_OVERLAY_TRACK_INDEX: int = 2


def choose_overlay_path(
    request: OverlayItemRequest,
    support: FusionSupport,
    geometry: OverlayGeometry,
) -> OverlayPathDecision:
    """Choose the verified path for one titled item; fallbacks are explicit."""

    if request.fusion_graph is not None:
        raise OverlayPathError(
            "complex_graph_blocked",
            f"{request.item_id}: arbitrary Fusion graph payloads are blocked; only the "
            f"published control surface {PUBLISHED_CONTROLS} is ever applied",
        )
    region = geometry.region_for(request.anchor)
    common = {
        "role": request.role,
        "text": request.text,
        "record_span": request.record_span,
        "anchor": request.anchor,
        "region": region,
        "asset_path": request.asset_path,
        "asset_sha256": request.asset_sha256,
    }
    if request.declared_path != "fusion_template":
        return OverlayPathDecision(
            item_id=request.item_id,
            path="external_media",
            reason="profile_declared_external",
            **common,
        )
    unsupported = tuple(
        control.control_id
        for control in request.fusion_controls
        if control.control_id not in support.published_controls
    )
    if unsupported:
        return OverlayPathDecision(
            item_id=request.item_id,
            path="external_media",
            reason="unsupported_control_explicit_fallback",
            fallback_controls=unsupported,
            **common,
        )
    if not support.supported:
        return OverlayPathDecision(
            item_id=request.item_id,
            path="external_media",
            reason="fusion_unsupported_explicit_fallback",
            **common,
        )
    if support.template_probe_sha256 is None:
        raise OverlayPathError(
            "missing_template_binding",
            f"{request.item_id}: fusion is live-supported but the probe-verified template "
            "binding hash is missing",
        )
    if request.font_family is None or request.font_evidence_sha256 is None:
        raise OverlayPathError(
            "missing_font_binding",
            f"{request.item_id}: the fusion path requires a hash-bound font declaration",
        )
    return OverlayPathDecision(
        item_id=request.item_id,
        path="fusion_template",
        reason="profile_declared_fusion",
        template_name=support.template_name,
        template_probe_sha256=support.template_probe_sha256,
        font_family=request.font_family,
        font_evidence_sha256=request.font_evidence_sha256,
        controls=request.fusion_controls,
        **common,
    )


def presence_anchors(
    span_start: int, span_end: int, *, min_coverage_percent: int = 80
) -> tuple[PresenceAnchor, ...]:
    """Anchor frames proving duration: uncovered before/after, covered inside."""

    anchors: list[PresenceAnchor] = []
    if span_start > 0:
        anchors.append(
            PresenceAnchor(
                frame_index=span_start - 1,
                expect_covered=False,
                min_coverage_percent=min_coverage_percent,
            )
        )
    anchors.append(
        PresenceAnchor(
            frame_index=span_start,
            expect_covered=True,
            min_coverage_percent=min_coverage_percent,
        )
    )
    anchors.append(
        PresenceAnchor(
            frame_index=span_end - 1,
            expect_covered=True,
            min_coverage_percent=min_coverage_percent,
        )
    )
    anchors.append(
        PresenceAnchor(
            frame_index=span_end,
            expect_covered=False,
            min_coverage_percent=min_coverage_percent,
        )
    )
    return tuple(anchors)


def build_overlay_section(
    item_requests: tuple[OverlayItemRequest, ...],
    support: FusionSupport,
    geometry: OverlayGeometry,
    *,
    rendered: dict[str, OverlayRenderedMedia],
    overlay_track_index: int = DEFAULT_OVERLAY_TRACK_INDEX,
) -> OverlaySection:
    """Decide every item, bind rendered overlay media, and derive anchors."""

    decisions = tuple(
        choose_overlay_path(request, support, geometry) for request in item_requests
    )
    bound: list[OverlayRenderedMedia] = []
    anchors: list[PresenceAnchor] = []
    for decision in decisions:
        if decision.path != "external_media":
            continue
        media = rendered.get(decision.item_id)
        if media is None:
            raise OverlayPathError(
                "overlay_binding_missing",
                f"{decision.item_id}: external path has no rendered overlay media",
            )
        bound.append(media)
        anchors.extend(
            presence_anchors(
                decision.record_span.start_frame, decision.record_span.end_frame
            )
        )
    return OverlaySection(
        decisions=decisions,
        geometry=geometry,
        overlay_track_index=overlay_track_index,
        rendered=tuple(bound),
        anchors=tuple(anchors),
    )


def verify_placement_readback(
    decision: OverlayPathDecision,
    readback: OverlayPlacementReadback,
    *,
    track_index: int,
    frame_origin: int,
) -> None:
    """Typed placement gate: wrong track or wrong span never passes."""

    expected_start = frame_origin + decision.record_span.start_frame
    if (
        readback.track_type != "video"
        or readback.track_index != track_index
        or readback.record_start != expected_start
    ):
        raise OverlayPathError(
            "wrong_track",
            f"{decision.item_id}: expected video/{track_index} at record {expected_start}, "
            f"observed {readback.track_type}/{readback.track_index} at "
            f"{readback.record_start}",
        )
    expected_end = frame_origin + decision.record_span.end_frame
    if readback.record_end != expected_end:
        raise OverlayPathError(
            "wrong_duration",
            f"{decision.item_id}: expected record end {expected_end}, observed "
            f"{readback.record_end}",
        )


def require_pixel_evidence(
    item_id: str, *, pixels_verified: bool, frames_extracted: int
) -> None:
    """API-only success never suffices: pixels or typed failure."""

    if not pixels_verified or frames_extracted <= 0:
        raise OverlayPathError(
            "api_only_success",
            f"{item_id}: placement claimed success without rendered pixel evidence "
            f"(frames={frames_extracted}, pixels_verified={pixels_verified})",
        )


def fusion_support_from_matrix(findings_path: Path) -> FusionSupport:
    """Derive the Fusion verdict from the published probe findings (fail-closed)."""

    if not findings_path.is_file():
        return _unsupported()
    try:
        matrix = CapabilityMatrix.model_validate_json(findings_path.read_bytes())
    except OSError:
        return _unsupported()
    rows = {finding.finding: finding for finding in matrix.findings}
    placement = rows.get(PLACEMENT_FINDING)
    controls = rows.get(CONTROL_FINDING)
    if (
        placement is None
        or controls is None
        or not placement.live_verified
        or not controls.live_verified
    ):
        return _unsupported()
    template_sha = controls.evidence_refs[0].sha256 if controls.evidence_refs else None
    return FusionSupport(
        supported=True,
        template_name="Text+",
        template_probe_sha256=template_sha,
        published_controls=PUBLISHED_CONTROLS,
    )


def _unsupported() -> FusionSupport:
    return FusionSupport(
        supported=False,
        template_name="Text+",
        template_probe_sha256=None,
        published_controls=PUBLISHED_CONTROLS,
    )


def matrix_supports_fusion(matrix: CapabilityMatrix) -> bool:
    rows = {finding.finding: finding for finding in matrix.findings}
    placement = rows.get(PLACEMENT_FINDING)
    controls = rows.get(CONTROL_FINDING)
    return (
        placement is not None
        and controls is not None
        and placement.live_verified
        and controls.live_verified
    )


__all__ = [
    "CONTROL_FINDING",
    "DEFAULT_OVERLAY_TRACK_INDEX",
    "PLACEMENT_FINDING",
    "build_overlay_section",
    "choose_overlay_path",
    "fusion_support_from_matrix",
    "matrix_supports_fusion",
    "presence_anchors",
    "require_pixel_evidence",
    "verify_placement_readback",
]
