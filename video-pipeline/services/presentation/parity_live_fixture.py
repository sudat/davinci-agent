"""Parity live fixture compilation (Todo 61): one manifest, both renders.

The SAME hash-sealed Presentation Manifest the offline tests use, built
from the frozen brand-A fixture (Edit Plan/Timeline IR, resolved profile,
rights-gated registry, Job-start snapshot), plus the observation tables
both render sides anchor to. Nothing is derived from render output.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from services.contracts.edit_plan_0c import (
    EditPlan0C,
    EditPlanBody0C,
    EditPlanItem0C,
    EditSourceRef0C,
)
from services.contracts.primitives import (
    Producer,
    RationalFrameRate,
    RecordFrameSpan,
    SourceFrameSpan,
    SourceRef,
)
from services.contracts.serialization import artifact_content_hash
from services.contracts.timeline_ir import (
    TimelineIrProduction,
    TimelineItem0C,
    TimelineTrackProduction,
    TrackRef0C,
)
from services.fixtures.manifest_phase3 import Phase3FixtureManifest
from services.presentation.asset_registry import (
    RegistrySnapshot,
    registry_from_phase3_manifests,
)
from services.presentation.manifest import (
    PresentationManifest,
    compile_presentation_manifest,
)
from services.presentation.models import (
    EpisodePresentationProfile,
    ResolvedPresentationProfile,
    SystemPresentationProfile,
)
from services.presentation.parity_models import (
    CueRegionObservation,
    ItemPlacementObservation,
    PlacementObservation,
)
from services.presentation.profiles import resolve_presentation_profile
from services.presentation.snapshot import freeze_job_presentation

MANIFEST_DIR: Path = Path("tests/fixtures/manifests/phase-3")
JOB_DATE = "2026-01-01"
JOB_TERRITORY = "WORLDWIDE"


def _rate(base: Phase3FixtureManifest) -> RationalFrameRate:
    return RationalFrameRate(
        num=base.editorial.frame_rate_num, den=base.editorial.frame_rate_den
    )


def edit_plan_for(base: Phase3FixtureManifest) -> EditPlan0C:
    rate = _rate(base)
    items = tuple(
        EditPlanItem0C(
            item_id=row.item_id,
            kind=row.kind,
            source_id=base.editorial.source_id,
            span=SourceFrameSpan(
                start_frame=row.source_start, end_frame=row.source_end, rate=rate
            ),
            track_index=row.track_index,
            av_link_id=row.av_link_id,
            subtitle_text=row.subtitle_text,
        )
        for row in base.editorial.base_records
    )
    draft = EditPlan0C(
        artifact_id="edit-plan-parity-live",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=Producer(name="parity-live", version="v1"),
        inputs=(),
        frame_rate=rate,
        plan=EditPlanBody0C(
            plan_version="v1",
            edit_source=EditSourceRef0C(
                source_id=base.editorial.source_id,
                total_frames=base.editorial.total_frames,
            ),
            items=items,
        ),
    )
    sealed = draft.model_copy(update={"content_hash": artifact_content_hash(draft)})
    return cast("EditPlan0C", sealed)


def timeline_ir_for(base: Phase3FixtureManifest) -> TimelineIrProduction:
    rate = _rate(base)
    grouped: dict[
        tuple[Literal["video", "audio", "subtitle"], int], list[TimelineItem0C]
    ] = {}
    for row in base.editorial.base_records:
        grouped.setdefault((row.kind, row.track_index), []).append(
            TimelineItem0C(
                item_id=row.item_id,
                kind=row.kind,
                source=SourceRef(
                    source_id=base.editorial.source_id,
                    span=SourceFrameSpan(
                        start_frame=row.source_start, end_frame=row.source_end, rate=rate
                    ),
                ),
                record_span=RecordFrameSpan(
                    start_frame=row.record_start, end_frame=row.record_end
                ),
                av_link_id=row.av_link_id,
                subtitle_text=row.subtitle_text,
            )
        )
    tracks = tuple(
        TimelineTrackProduction(
            track=TrackRef0C(kind=kind_index[0], index=kind_index[1]),
            items=tuple(items),
        )
        for kind_index, items in sorted(grouped.items())
    )
    draft = TimelineIrProduction(
        artifact_id="timeline-ir-parity-live",
        artifact_type="timeline_ir_v1",
        schema_version="timeline-ir-v1",
        content_hash="0" * 64,
        producer=Producer(name="parity-live", version="v1"),
        inputs=(),
        rate=rate,
        tracks=tracks,
    )
    return cast(
        "TimelineIrProduction",
        draft.model_copy(update={"content_hash": artifact_content_hash(draft)}),
    )


@dataclass(frozen=True, slots=True)
class ParityFixturePlan:
    """The single manifest both live renders anchor to, plus its inputs."""

    manifest: PresentationManifest
    timeline_ir: TimelineIrProduction
    profile: ResolvedPresentationProfile
    registry: RegistrySnapshot


def compile_parity_fixture(manifest_dir: Path = MANIFEST_DIR) -> ParityFixturePlan:
    base = Phase3FixtureManifest.model_validate_json(
        (manifest_dir / "p3-brand-a.json").read_bytes()
    )
    registry = registry_from_phase3_manifests(manifest_dir)
    catalog = tuple(sorted(entry.asset_id for entry in registry.entries))
    system = {
        "asset_catalog": list(catalog),
        "subtitle_style": base.presentation.subtitle_style.model_dump(),
        "color_profile": base.presentation.color_profile.model_dump(),
        "audio": base.presentation.audio.model_dump(),
        "placement": base.presentation.placement.model_dump(),
        "asset_bindings": [
            {"kind": asset.kind, "asset_id": f"{base.fixture_id}:{asset.kind}"}
            for asset in base.presentation.assets
        ],
    }
    profile = resolve_presentation_profile(
        SystemPresentationProfile.model_validate(system),
        episode=EpisodePresentationProfile(episode_id="episode-parity-live"),
        registry=registry,
    )
    snapshot = freeze_job_presentation(
        profile,
        registry,
        job_date=JOB_DATE,
        job_territory=JOB_TERRITORY,
        assets_root=Path(),
    )
    edit_plan = edit_plan_for(base)
    timeline_ir = timeline_ir_for(base)
    manifest = compile_presentation_manifest(
        edit_plan,
        timeline_ir,
        profile,
        registry,
        job_snapshot=snapshot,
        assets_root=Path(),
    )
    return ParityFixturePlan(
        manifest=manifest, timeline_ir=timeline_ir, profile=profile, registry=registry
    )


def item_observations(
    manifest: PresentationManifest,
) -> tuple[ItemPlacementObservation, ...]:
    return tuple(
        ItemPlacementObservation(
            item_id=item.item_id,
            record_start=item.record_start,
            record_end=item.record_end,
        )
        for track in manifest.editorial.tracks
        for item in track.items
    )


def placement_observation(manifest: PresentationManifest) -> PlacementObservation:
    placement = manifest.placement
    return PlacementObservation(
        intro_start_frame=placement.intro_record_span.start_frame,
        intro_end_frame=placement.intro_record_span.end_frame,
        outro_start_frame=placement.outro_record_span.start_frame,
        outro_end_frame=placement.outro_record_span.end_frame,
        safe_area_margin_px=placement.safe_area_margin_px,
    )


def cue_observations(
    manifest: PresentationManifest, timeline_ir: TimelineIrProduction
) -> tuple[CueRegionObservation, ...]:
    texts = {
        item.item_id: item.subtitle_text
        for track in timeline_ir.tracks
        for item in track.items
        if item.kind == "subtitle" and item.subtitle_text
    }
    style = manifest.style
    cues: list[CueRegionObservation] = []
    for track in manifest.editorial.tracks:
        if track.kind != "subtitle":
            continue
        for item in track.items:
            text = texts.get(item.item_id)
            if text is None:
                raise ValueError(
                    f"subtitle item {item.item_id} carries no cue text in the IR"
                )
            cues.append(
                CueRegionObservation(
                    item_id=item.item_id,
                    text=text,
                    lines=(text,),
                    start_frame=item.record_start,
                    end_frame=item.record_end,
                    style_id=style.style_id,
                    font_size_px=style.font_size_px,
                    margin_bottom_px=style.margin_bottom_px,
                    primary_color_hex=style.primary_color_hex,
                )
            )
    return tuple(cues)


__all__ = [
    "MANIFEST_DIR",
    "ParityFixturePlan",
    "compile_parity_fixture",
    "cue_observations",
    "edit_plan_for",
    "item_observations",
    "placement_observation",
    "timeline_ir_for",
]
