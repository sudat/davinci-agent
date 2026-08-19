"""Presentation Manifest compilation (PRD 1320-1418, 1642-1657).

The SAME Episode / Edit Plan / Timeline IR maps to presentation items via
the resolved profile's bindings: subtitle style refs, intro/outro placement
refs bound to IR record positions, audio tone/SE refs, color refs, and the
resolved asset content hashes. Compilation is deterministic (same inputs →
byte-identical canonical bytes), gated on the Job-start snapshot (any drift
blocks), and the result is sealed over canonical bytes. Editorial content is
carried as an invariant fingerprint: swapping profiles changes presentation
fields only.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from services.contracts.primitives import (
    Identifier,
    RationalFrameRate,
    RecordFrameSpan,
    Sha256,
    StrictModel,
)
from services.contracts.serialization import GENESIS_SHA256, canonical_json_bytes
from services.presentation.asset_registry import RegistrySnapshot, check_rights
from services.presentation.models import (
    PHASE_3_PRESENTATION_KINDS,
    Anchor,
    AudioBrandConfig,
    ColorProfileConfig,
    PlacementConfig,
    PresentationAssetKind,
    ResolvedPresentationProfile,
    SubtitleStyleConfig,
)
from services.presentation.snapshot import (
    JobPresentationSnapshot,
    verify_job_presentation,
)

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIrProduction

type EditorialTrackKind = Literal["video", "audio", "subtitle"]


class ManifestError(ValueError):
    """The inputs are inconsistent or the profile bindings are incomplete."""


class EditorialItemBinding(StrictModel):
    item_id: Identifier
    record_start: int = Field(ge=0, strict=True)
    record_end: int = Field(gt=0, strict=True)


class EditorialTrackBinding(StrictModel):
    kind: EditorialTrackKind
    index: int = Field(gt=0, strict=True)
    items: tuple[EditorialItemBinding, ...] = Field(min_length=1)


class EditorialBinding(StrictModel):
    """The NLE-neutral editorial summary a manifest is bound to (invariant)."""

    frame_rate: RationalFrameRate
    total_frames: int = Field(gt=0, strict=True)
    tracks: tuple[EditorialTrackBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_tracks(self) -> EditorialBinding:
        refs = [(track.kind, track.index) for track in self.tracks]
        if len(set(refs)) != len(refs):
            raise ValueError("each editorial track appears at most once")
        return self


class AssetContentRef(StrictModel):
    """An asset fixed by content hash; ids/paths live in the registry snapshot."""

    sha256: Sha256


class PlacementBinding(StrictModel):
    """Placement refs bound to absolute timeline record positions."""

    intro_duration_frames: int = Field(gt=0, strict=True)
    outro_duration_frames: int = Field(gt=0, strict=True)
    logo_anchor: Anchor
    overlay_anchor: Anchor
    safe_area_margin_px: int = Field(ge=0, strict=True)
    intro_record_span: RecordFrameSpan
    outro_record_span: RecordFrameSpan

    @model_validator(mode="after")
    def require_disjoint_spans(self) -> PlacementBinding:
        if self.outro_record_span.start_frame < self.intro_record_span.end_frame:
            raise ValueError("intro and outro placements must not overlap")
        return self


class PresentationManifest(StrictModel):
    schema_version: Literal["presentation-manifest-v1"] = "presentation-manifest-v1"
    episode_id: Identifier
    edit_plan_artifact_id: Identifier
    edit_plan_content_sha256: Sha256
    timeline_ir_artifact_id: Identifier
    timeline_ir_content_sha256: Sha256
    profile_snapshot_sha256: Sha256
    registry_snapshot_sha256: Sha256
    editorial_fingerprint: Sha256
    editorial: EditorialBinding
    style: SubtitleStyleConfig
    color: ColorProfileConfig
    audio: AudioBrandConfig
    placement: PlacementBinding
    assets: dict[PresentationAssetKind, AssetContentRef]
    manifest_sha256: Sha256

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    def content_hash(self) -> Sha256:
        zeroed = self.model_copy(update={"manifest_sha256": GENESIS_SHA256})
        return hashlib.sha256(canonical_json_bytes(zeroed)).hexdigest()

    def verify_hash(self) -> bool:
        return self.manifest_sha256 == self.content_hash()


def _editorial_binding(timeline_ir: TimelineIrProduction) -> EditorialBinding:
    tracks = tuple(
        EditorialTrackBinding(
            kind=track.track.kind,
            index=track.track.index,
            items=tuple(
                sorted(
                    (
                        EditorialItemBinding(
                            item_id=item.item_id,
                            record_start=item.record_span.start_frame,
                            record_end=item.record_span.end_frame,
                        )
                        for item in track.items
                    ),
                    key=lambda item: (item.record_start, item.item_id),
                )
            ),
        )
        for track in sorted(
            timeline_ir.tracks, key=lambda track: (track.track.kind, track.track.index)
        )
    )
    total_frames = max(
        item.record_span.end_frame for track in timeline_ir.tracks for item in track.items
    )
    return EditorialBinding(
        frame_rate=timeline_ir.rate, total_frames=total_frames, tracks=tracks
    )


def _placement_binding(placement: PlacementConfig, total_frames: int) -> PlacementBinding:
    intro = RecordFrameSpan(
        start_frame=0, end_frame=placement.intro_duration_frames
    )
    outro = RecordFrameSpan(
        start_frame=total_frames - placement.outro_duration_frames,
        end_frame=total_frames,
    )
    if outro.start_frame < intro.end_frame:
        raise ManifestError("timeline is too short for the intro/outro placements")
    return PlacementBinding(
        intro_duration_frames=placement.intro_duration_frames,
        outro_duration_frames=placement.outro_duration_frames,
        logo_anchor=placement.logo_anchor,
        overlay_anchor=placement.overlay_anchor,
        safe_area_margin_px=placement.safe_area_margin_px,
        intro_record_span=intro,
        outro_record_span=outro,
    )


def compile_presentation_manifest(  # noqa: PLR0913 (signature fixed by the Todo-55 contract)
    edit_plan: EditPlan0C,
    timeline_ir: TimelineIrProduction,
    resolved_profile: ResolvedPresentationProfile,
    registry_snapshot: RegistrySnapshot,
    *,
    job_snapshot: JobPresentationSnapshot,
    assets_root: Path,
) -> PresentationManifest:
    """Compile the hash-sealed manifest; any snapshot or rights drift blocks."""

    verify_job_presentation(job_snapshot, resolved_profile, registry_snapshot, assets_root)
    if edit_plan.frame_rate != timeline_ir.rate:
        raise ManifestError("edit plan and timeline IR frame rates disagree")
    ir_item_ids = {
        item.item_id for track in timeline_ir.tracks for item in track.items
    }
    plan_item_ids = {item.item_id for item in edit_plan.plan.items}
    absent = sorted(plan_item_ids - ir_item_ids)
    if absent:
        raise ManifestError(f"edit plan items absent from the timeline IR: {absent}")

    editorial = _editorial_binding(timeline_ir)
    assets: dict[PresentationAssetKind, AssetContentRef] = {}
    for binding in resolved_profile.asset_bindings:
        entry = check_rights(
            registry_snapshot,
            binding.asset_id,
            usage=binding.kind,
            territory=job_snapshot.job_territory,
            at_job=job_snapshot.job_date,
        )
        assets[binding.kind] = AssetContentRef(sha256=entry.sha256)
    if set(assets) != set(PHASE_3_PRESENTATION_KINDS):
        raise ManifestError("asset bindings must cover exactly the six kinds")

    editorial_fingerprint = hashlib.sha256(
        canonical_json_bytes(editorial)
    ).hexdigest()
    draft = PresentationManifest(
        episode_id=resolved_profile.episode_id,
        edit_plan_artifact_id=edit_plan.artifact_id,
        edit_plan_content_sha256=edit_plan.content_hash,
        timeline_ir_artifact_id=timeline_ir.artifact_id,
        timeline_ir_content_sha256=timeline_ir.content_hash,
        profile_snapshot_sha256=resolved_profile.content_hash(),
        registry_snapshot_sha256=registry_snapshot.content_hash(),
        editorial_fingerprint=editorial_fingerprint,
        editorial=editorial,
        style=resolved_profile.subtitle_style,
        color=resolved_profile.color_profile,
        audio=resolved_profile.audio,
        placement=_placement_binding(
            resolved_profile.placement, editorial.total_frames
        ),
        assets=assets,
        manifest_sha256=GENESIS_SHA256,
    )
    return draft.model_copy(update={"manifest_sha256": draft.content_hash()})
