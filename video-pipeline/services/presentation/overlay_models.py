"""Overlay-path models (Todo 58): the dual-path overlay/title strategy table.

Every titled item from the styled presentation is applied through exactly
one verified path: the FUSION template path (only when the live title probe
verified template placement AND published-control readback) or the EXTERNAL
transparent-media path (the default). Decisions are hash-bound to the
registry asset, the probe-verified template, and the rendered overlay media;
an unverified Fusion request falls back to the external path with an
explicit recorded reason — never silently. Arbitrary Fusion graph payloads
are blocked outright: only the published control surface is ever applied.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Identifier, RecordFrameSpan, Sha256, StrictModel

type OverlayPathKind = Literal["fusion_template", "external_media"]
type TitledRole = Literal["keyword_overlay", "chapter_title"]
type TitledAnchor = Literal["top-left", "top-right", "bottom-left", "bottom-right"]
type OverlayPathReason = Literal[
    "profile_declared_fusion",
    "profile_declared_external",
    "fusion_unsupported_explicit_fallback",
    "unsupported_control_explicit_fallback",
]
type OverlayPathErrorCode = Literal[
    "missing_font_binding",
    "missing_template_binding",
    "complex_graph_blocked",
    "wrong_track",
    "wrong_duration",
    "api_only_success",
    "rendered_presence_missing",
    "rendered_region_out_of_frame",
    "overlay_media_hash_drift",
    "overlay_binding_missing",
]

PUBLISHED_CONTROLS: tuple[str, ...] = ("StyledText", "Size", "Center")


class OverlayPathError(ValueError):
    """Typed overlay-path failure; ``code`` carries the machine cause."""

    def __init__(self, code: OverlayPathErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _tuple[Value](value: list[Value] | tuple[Value, ...]) -> tuple[Value, ...]:
    return tuple(value)


type OverlaySequence[Value] = Annotated[tuple[Value, ...], BeforeValidator(_tuple)]

RgbChannel = Annotated[int, Field(ge=0, le=255, strict=True)]
type Rgb = tuple[RgbChannel, RgbChannel, RgbChannel]


class OverlayRegion(StrictModel):
    """The pixel rectangle the overlay occupies on the timeline frame."""

    x: int = Field(ge=0, strict=True)
    y: int = Field(ge=0, strict=True)
    width: int = Field(gt=0, strict=True)
    height: int = Field(gt=0, strict=True)


class OverlayGeometry(StrictModel):
    """Timeline pixel geometry shared by every overlay placement decision."""

    timeline_width: int = Field(gt=0, strict=True)
    timeline_height: int = Field(gt=0, strict=True)
    safe_margin_px: int = Field(ge=0, strict=True)
    region_width_px: int = Field(gt=0, strict=True)
    region_height_px: int = Field(gt=0, strict=True)

    def region_for(self, anchor: TitledAnchor) -> OverlayRegion:
        right = self.timeline_width - self.safe_margin_px - self.region_width_px
        bottom = self.timeline_height - self.safe_margin_px - self.region_height_px
        x = self.safe_margin_px if anchor.endswith("left") else right
        y = self.safe_margin_px if anchor.startswith("top") else bottom
        if x < 0 or y < 0:
            raise OverlayPathError(
                "rendered_region_out_of_frame",
                f"{anchor} region {self.region_width_px}x{self.region_height_px} + margin "
                f"{self.safe_margin_px} does not fit {self.timeline_width}x{self.timeline_height}",
            )
        return OverlayRegion(x=x, y=y, width=self.region_width_px, height=self.region_height_px)


class FusionSupport(StrictModel):
    """The evidence-bound Fusion-title capability verdict from the live probe."""

    supported: bool
    template_name: str = Field(min_length=1)
    template_probe_sha256: Sha256 | None
    published_controls: OverlaySequence[str]
    font_family: str | None = None
    font_evidence_sha256: Sha256 | None = None


class OverlayControlValue(StrictModel):
    """One published-control value: exactly one of text / number / point."""

    text: str | None = None
    number: float | None = None
    point: tuple[float, float] | None = None

    @model_validator(mode="after")
    def require_exactly_one(self) -> OverlayControlValue:
        set_fields = [
            name
            for name, value in (
                ("text", self.text),
                ("number", self.number),
                ("point", self.point),
            )
            if value is not None
        ]
        if len(set_fields) != 1:
            raise PydanticCustomError(
                "control_value", "exactly one of text/number/point must be set"
            )
        return self


class OverlayControlRequest(StrictModel):
    """A request to set one published Fusion control on the placed title."""

    control_id: str = Field(min_length=1, strict=True)
    value: OverlayControlValue


class OverlayItemRequest(StrictModel):
    """One titled item to apply: declared path, bindings, and control values."""

    item_id: Identifier
    role: TitledRole
    text: str = Field(min_length=1, strict=True)
    record_span: RecordFrameSpan
    anchor: TitledAnchor
    asset_path: str = Field(min_length=1)
    asset_sha256: Sha256
    declared_path: OverlayPathKind
    font_family: str | None = None
    font_evidence_sha256: Sha256 | None = None
    fusion_controls: OverlaySequence[OverlayControlRequest] = ()
    fusion_graph: str | None = None


class OverlayPathDecision(StrictModel):
    """The chosen, hash-bound path for one titled item."""

    item_id: Identifier
    path: OverlayPathKind
    reason: OverlayPathReason
    role: TitledRole
    text: str = Field(min_length=1, strict=True)
    record_span: RecordFrameSpan
    anchor: TitledAnchor
    region: OverlayRegion
    asset_path: str = Field(min_length=1)
    asset_sha256: Sha256
    template_name: str | None = None
    template_probe_sha256: Sha256 | None = None
    font_family: str | None = None
    font_evidence_sha256: Sha256 | None = None
    controls: OverlaySequence[OverlayControlRequest] = ()
    fallback_controls: OverlaySequence[str] = ()


class OverlayPlacementReadback(StrictModel):
    """Observed post-placement readback for one overlay media item."""

    item_id: Identifier
    track_type: str
    track_index: int = Field(gt=0, strict=True)
    record_start: int = Field(ge=0, strict=True)
    record_end: int = Field(ge=0, strict=True)


class PresenceAnchor(StrictModel):
    """One frame whose region coverage proves presence, position, or duration."""

    frame_index: int = Field(ge=0, strict=True)
    expect_covered: bool
    min_coverage_percent: int = Field(ge=1, le=100, strict=True)


class OverlayRenderedMedia(StrictModel):
    """The pinned-ffmpeg transparent overlay media bound to one decision."""

    item_id: Identifier
    path: str = Field(min_length=1)
    sha256: Sha256
    duration_frames: int = Field(gt=0, strict=True)
    expected_rgb: Rgb


class OverlaySection(StrictModel):
    """The overlay strategy table carried inside a Resolve Package (Todo 58)."""

    schema_version: Literal["overlay-paths-v1"] = "overlay-paths-v1"
    decisions: OverlaySequence[OverlayPathDecision] = Field(min_length=1)
    geometry: OverlayGeometry
    overlay_track_index: int = Field(gt=0, strict=True)
    rendered: OverlaySequence[OverlayRenderedMedia] = ()
    anchors: OverlaySequence[PresenceAnchor] = Field(min_length=1)


__all__ = [
    "PUBLISHED_CONTROLS",
    "FusionSupport",
    "OverlayControlRequest",
    "OverlayControlValue",
    "OverlayGeometry",
    "OverlayItemRequest",
    "OverlayPathDecision",
    "OverlayPathError",
    "OverlayPathKind",
    "OverlayPathReason",
    "OverlayPlacementReadback",
    "OverlayRegion",
    "OverlayRenderedMedia",
    "OverlaySection",
    "PresenceAnchor",
    "Rgb",
    "TitledAnchor",
    "TitledRole",
]
