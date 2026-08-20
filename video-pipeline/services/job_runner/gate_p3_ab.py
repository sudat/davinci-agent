"""Offline A/B compilation for the Phase-3 gate (Todo 62).

The SAME frozen Edit Plan and Timeline IR (brand-A fixture editorial, the
golden-pinned structure) resolves twice against the frozen registry:
snapshot A (system profile of brand A) and snapshot B (the same system
profile narrowed by brand B's channel profile — the declared swap
mechanism). Both manifests are compiled through the REAL Todo-55 compiler
and written to the evidence tree; nothing is derived from build output.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.contracts.serialization import canonical_json_bytes
from services.contracts.timeline_ir import TimelineItem0C
from services.fixtures.manifest_phase3 import (
    PHASE_3_FIXTURE_IDS,
    Phase3FixtureManifest,
    editorial_structure_bytes,
)
from services.foundation_io import atomic_write
from services.job_runner.gate_p3_models import P3StructRow
from services.presentation.asset_registry import (
    RegistrySnapshot,
    registry_from_phase3_manifests,
)
from services.presentation.manifest import PresentationManifest, compile_presentation_manifest
from services.presentation.models import (
    ChannelPresentationProfile,
    EpisodePresentationProfile,
    ResolvedPresentationProfile,
    SystemPresentationProfile,
)
from services.presentation.parity_live_fixture import edit_plan_for, timeline_ir_for
from services.presentation.profiles import resolve_presentation_profile
from services.presentation.snapshot import JobPresentationSnapshot, freeze_job_presentation

if TYPE_CHECKING:
    from services.contracts.edit_plan_0c import EditPlan0C
    from services.contracts.timeline_ir import TimelineIrProduction

MANIFEST_DIR: Final = Path("tests/fixtures/manifests/phase-3")
GOLDEN_DIR: Final = Path("tests/goldens/reference/phase-3")
JOB_DATE: Final = "2026-01-01"
JOB_TERRITORY: Final = "WORLDWIDE"
SEAL_FIELDS: Final = frozenset({"manifest_sha256", "profile_snapshot_sha256"})
EDIT_PLAN_NAME: Final = "edit-plan.json"
TIMELINE_IR_NAME: Final = "timeline-ir.json"
REGISTRY_NAME: Final = "registry.json"


@dataclass(frozen=True, slots=True)
class AbPlan:
    """The frozen inputs plus both compiled snapshots (A then B)."""

    edit_plan: EditPlan0C
    timeline_ir: TimelineIrProduction
    registry: RegistrySnapshot
    manifests: dict[str, PresentationManifest]
    profiles: dict[str, ResolvedPresentationProfile]
    snapshots: dict[str, JobPresentationSnapshot]


def _keys_of(brand: Phase3FixtureManifest) -> dict[str, object]:
    presentation = brand.presentation
    return {
        "subtitle_style": dict(presentation.subtitle_style.model_dump()),
        "color_profile": dict(presentation.color_profile.model_dump()),
        "audio": dict(presentation.audio.model_dump()),
        "placement": dict(presentation.placement.model_dump()),
        "asset_bindings": [
            {"kind": asset.kind, "asset_id": f"{brand.fixture_id}:{asset.kind}"}
            for asset in presentation.assets
        ],
    }


def compile_ab(manifest_dir: Path = MANIFEST_DIR) -> AbPlan:
    """Compile snapshot A then snapshot B from the SAME frozen editorial."""

    base_a = Phase3FixtureManifest.model_validate_json(
        (manifest_dir / "p3-brand-a.json").read_bytes()
    )
    brand_b = Phase3FixtureManifest.model_validate_json(
        (manifest_dir / "p3-brand-b.json").read_bytes()
    )
    registry = registry_from_phase3_manifests(manifest_dir)
    catalog = tuple(sorted(entry.asset_id for entry in registry.entries))
    system = SystemPresentationProfile.model_validate(
        {"asset_catalog": list(catalog), **_keys_of(base_a)}
    )
    channel_b = ChannelPresentationProfile.model_validate(
        {"channel_id": f"channel-{brand_b.brand_id}", **_keys_of(brand_b)}
    )
    profile_a = resolve_presentation_profile(
        system,
        episode=EpisodePresentationProfile(episode_id="episode-phase-3-gate"),
        registry=registry,
    )
    profile_b = resolve_presentation_profile(
        system,
        episode=EpisodePresentationProfile(episode_id="episode-phase-3-gate"),
        channel=channel_b,
        registry=registry,
    )
    snapshot_a = freeze_job_presentation(
        profile_a, registry, job_date=JOB_DATE, job_territory=JOB_TERRITORY, assets_root=Path()
    )
    snapshot_b = freeze_job_presentation(
        profile_b, registry, job_date=JOB_DATE, job_territory=JOB_TERRITORY, assets_root=Path()
    )
    edit_plan = edit_plan_for(base_a)
    timeline_ir = timeline_ir_for(base_a)
    manifest_a = compile_presentation_manifest(
        edit_plan,
        timeline_ir,
        profile_a,
        registry,
        job_snapshot=snapshot_a,
        assets_root=Path(),
    )
    manifest_b = compile_presentation_manifest(
        edit_plan,
        timeline_ir,
        profile_b,
        registry,
        job_snapshot=snapshot_b,
        assets_root=Path(),
    )
    return AbPlan(
        edit_plan=edit_plan,
        timeline_ir=timeline_ir,
        registry=registry,
        manifests={"p3-brand-a": manifest_a, "p3-brand-b": manifest_b},
        profiles={"p3-brand-a": profile_a, "p3-brand-b": profile_b},
        snapshots={"p3-brand-a": snapshot_a, "p3-brand-b": snapshot_b},
    )


def structure_rows(timeline_ir: TimelineIrProduction) -> tuple[P3StructRow, ...]:
    """Decision ids, source spans, record spans, item count — the invariant."""

    return tuple(
        P3StructRow(
            item_id=item.item_id,
            kind=track.track.kind,
            track_index=track.track.index,
            source_start=item.source.span.start_frame,
            source_end=item.source.span.end_frame,
            record_start=item.record_span.start_frame,
            record_end=item.record_span.end_frame,
        )
        for track in timeline_ir.tracks
        for item in track.items
        if isinstance(item, TimelineItem0C)
    )


def structure_drift(a: tuple[P3StructRow, ...], b: tuple[P3StructRow, ...]) -> tuple[str, ...]:
    if len(a) != len(b):
        return (f"item-count {len(a)}!={len(b)}",)
    return tuple(
        f"{row_a.item_id}: {row_b.model_dump_json()}"
        for row_a, row_b in zip(a, b, strict=True)
        if row_a != row_b
    )


def record_span_drift(
    a: tuple[P3StructRow, ...], b: tuple[P3StructRow, ...]
) -> tuple[str, ...]:
    """Compare Decision ids + record spans + item count (manifest binding)."""

    if len(a) != len(b):
        return (f"item-count {len(a)}!={len(b)}",)
    return tuple(
        f"{row_a.item_id}: record {row_a.record_start}-{row_a.record_end}"
        f" != {row_b.record_start}-{row_b.record_end}"
        for row_a, row_b in zip(a, b, strict=True)
        if (row_a.item_id, row_a.record_start, row_a.record_end)
        != (row_b.item_id, row_b.record_start, row_b.record_end)
    )


def editorial_structure_sha(manifest_dir: Path = MANIFEST_DIR) -> str:
    shas = {
        hashlib.sha256(
            editorial_structure_bytes(
                Phase3FixtureManifest.model_validate_json(
                    (manifest_dir / f"{fixture_id}.json").read_bytes()
                ).editorial
            )
        ).hexdigest()
        for fixture_id in PHASE_3_FIXTURE_IDS
    }
    if len(shas) != 1:
        raise ValueError("the two brand fixtures declare different editorial structures")
    return shas.pop()


def manifest_payload(manifest: PresentationManifest) -> dict[str, object]:
    payload = json.loads(manifest.canonical_bytes())
    if not isinstance(payload, dict):
        raise TypeError("manifest payload is not an object")
    return payload


def _flatten(node: object, prefix: str = "") -> dict[str, object]:
    leaves: dict[str, object] = {}
    if isinstance(node, dict):
        for key in sorted(node):
            leaves.update(_flatten(node[key], f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            leaves.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        leaves[prefix] = node
    return leaves


def _translate(dim: str) -> str:
    namespace, _, rest = dim.partition(".")
    return f"assets.{rest}" if namespace == "asset" else dim


def declared_dimensions(golden_root: Path = GOLDEN_DIR) -> frozenset[str]:
    golden = json.loads((golden_root / "expected.json").read_bytes())
    declared = golden["ab_diff"]["declared_dimensions"]
    if not isinstance(declared, list):
        raise TypeError("golden declared_dimensions is not a list")
    return frozenset(_translate(str(dim)) for dim in declared)


def presentation_diff(
    manifest_a: PresentationManifest, manifest_b: PresentationManifest
) -> tuple[frozenset[str], frozenset[str]]:
    """Returns (observed diff leaf paths, declared diff leaf paths)."""

    leaves_a = _flatten(manifest_payload(manifest_a))
    leaves_b = _flatten(manifest_payload(manifest_b))
    observed = frozenset(
        path
        for path in set(leaves_a) | set(leaves_b)
        if leaves_a.get(path) != leaves_b.get(path)
    ) - SEAL_FIELDS
    return observed, declared_dimensions()


def write_ab_evidence(root: Path, plan: AbPlan) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    atomic_write(root / EDIT_PLAN_NAME, canonical_json_bytes(plan.edit_plan))
    atomic_write(root / TIMELINE_IR_NAME, canonical_json_bytes(plan.timeline_ir))
    atomic_write(root / REGISTRY_NAME, plan.registry.canonical_bytes())
    for snapshot_id in PHASE_3_FIXTURE_IDS:
        atomic_write(
            root / f"manifest-{snapshot_id}.json",
            plan.manifests[snapshot_id].canonical_bytes(),
        )
        atomic_write(
            root / f"profile-{snapshot_id}.json",
            plan.profiles[snapshot_id].canonical_bytes(),
        )
        atomic_write(
            root / f"job-snapshot-{snapshot_id}.json",
            plan.snapshots[snapshot_id].canonical_bytes(),
        )
    return root


__all__ = [
    "EDIT_PLAN_NAME",
    "GOLDEN_DIR",
    "JOB_DATE",
    "JOB_TERRITORY",
    "MANIFEST_DIR",
    "REGISTRY_NAME",
    "SEAL_FIELDS",
    "TIMELINE_IR_NAME",
    "AbPlan",
    "compile_ab",
    "declared_dimensions",
    "editorial_structure_sha",
    "manifest_payload",
    "presentation_diff",
    "record_span_drift",
    "structure_drift",
    "structure_rows",
    "write_ab_evidence",
]
