"""Todo-55 acceptance: deterministic Presentation Manifest compilation.

The SAME Episode/Edit Plan/Timeline IR compiles against profile A and profile
B; the manifests must differ in EXACTLY the frozen Todo-56 golden diff
dimensions (asserted against ``tests/goldens/reference/phase-3/expected.json``
— never derived from implementation output), be byte-identical on
double-compile, and refuse any post-snapshot drift (asset bytes, profile,
registry) with typed blocking errors.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Literal, cast

import pytest

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
    AssetEntry,
    RegistrySnapshot,
    register_assets,
    registry_from_phase3_manifests,
)
from services.presentation.manifest import (
    PresentationManifest,
    compile_presentation_manifest,
)
from services.presentation.models import (
    ChannelPresentationProfile,
    EpisodePresentationProfile,
    ResolvedPresentationProfile,
    SystemPresentationProfile,
)
from services.presentation.profiles import resolve_presentation_profile
from services.presentation.snapshot import (
    StaleSnapshotError,
    freeze_job_presentation,
    verify_job_presentation,
)

MANIFEST_DIR = Path("tests/fixtures/manifests/phase-3")
GOLDEN_PATH = Path("tests/goldens/reference/phase-3/expected.json")
ASSETS_ROOT = Path()
JOB_DATE = "2026-01-01"
JOB_TERRITORY = "WORLDWIDE"
SEAL_FIELDS = frozenset({"manifest_sha256", "profile_snapshot_sha256"})


def _load(fixture_id: str) -> Phase3FixtureManifest:
    return Phase3FixtureManifest.model_validate_json(
        (MANIFEST_DIR / f"{fixture_id}.json").read_bytes()
    )


def _edit_plan(base: Phase3FixtureManifest) -> EditPlan0C:
    rate = RationalFrameRate(
        num=base.editorial.frame_rate_num, den=base.editorial.frame_rate_den
    )
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
        artifact_id="edit-plan-p3",
        artifact_type="edit_plan_0c",
        schema_version="edit-plan-0c-v1",
        content_hash="0" * 64,
        producer=Producer(name="presentation-test", version="v1"),
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
    assert isinstance(sealed, EditPlan0C)
    return sealed


def _timeline_ir(base: Phase3FixtureManifest) -> TimelineIrProduction:
    rate = RationalFrameRate(
        num=base.editorial.frame_rate_num, den=base.editorial.frame_rate_den
    )
    grouped: dict[tuple[Literal["video", "audio", "subtitle"], int], list[TimelineItem0C]] = {}
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
            track=TrackRef0C(kind=kind_index[0], index=kind_index[1]), items=tuple(items)
        )
        for kind_index, items in sorted(grouped.items())
    )
    draft = TimelineIrProduction(
        artifact_id="timeline-ir-p3",
        artifact_type="timeline_ir_v1",
        schema_version="timeline-ir-v1",
        content_hash="0" * 64,
        producer=Producer(name="presentation-test", version="v1"),
        inputs=(),
        rate=rate,
        tracks=tracks,
    )
    sealed = draft.model_copy(update={"content_hash": artifact_content_hash(draft)})
    assert isinstance(sealed, TimelineIrProduction)
    return sealed


def _keys_of(manifest: Phase3FixtureManifest) -> dict[str, object]:
    presentation = manifest.presentation
    return {
        "subtitle_style": dict(presentation.subtitle_style.model_dump()),
        "color_profile": dict(presentation.color_profile.model_dump()),
        "audio": dict(presentation.audio.model_dump()),
        "placement": dict(presentation.placement.model_dump()),
        "asset_bindings": [
            {"kind": asset.kind, "asset_id": f"{manifest.fixture_id}:{asset.kind}"}
            for asset in presentation.assets
        ],
    }


def _system(brand: Phase3FixtureManifest, catalog: tuple[str, ...]) -> SystemPresentationProfile:
    return SystemPresentationProfile.model_validate(
        {"asset_catalog": list(catalog), **_keys_of(brand)}
    )


def _channel(brand: Phase3FixtureManifest) -> ChannelPresentationProfile:
    return ChannelPresentationProfile.model_validate(
        {"channel_id": f"channel-{brand.brand_id}", **_keys_of(brand)}
    )


def _extra_entry() -> AssetEntry:
    return AssetEntry.model_validate(
        {
            "asset_id": "extra-brand:tone",
            "sha256": "c" * 64,
            "path": "tests/fixtures/manifests/phase-3/assets/p3-brand-a/tone.wav",
            "usage": "tone",
            "territories": ("WORLDWIDE",),
            "effective_date": "1970-01-01",
            "expiry_date": None,
            "license_evidence_ref": "owner-created:test",
            "attribution": None,
            "content_id_notes": None,
            "approved": True,
        }
    )


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


def _field(data: object, key: str) -> object:
    return cast("dict[str, object]", data)[key]


def _assert_matches_golden(
    payload_a: dict[str, object], payload_b: dict[str, object], golden: dict[str, object]
) -> None:
    ab_diff = cast("dict[str, object]", golden["ab_diff"])
    declared = [str(dim) for dim in cast("list[object]", ab_diff["declared_dimensions"])]
    translated = {_translate(dim) for dim in declared}
    leaves_a = _flatten(payload_a)
    leaves_b = _flatten(payload_b)
    diffs = {
        path
        for path in set(leaves_a) | set(leaves_b)
        if leaves_a.get(path) != leaves_b.get(path)
    } - SEAL_FIELDS
    assert diffs == translated, f"undeclared or missing presentation drift: {diffs ^ translated}"

    for dim in declared:
        parts = dim.split(".")
        if parts[0] == "asset":
            path = f"assets.{parts[1]}.sha256"
            section = _field(_field(ab_diff, "asset_sha256"), parts[1])
        else:
            path = dim
            section = _field(_field(ab_diff, parts[0]), parts[1])
        assert _field(section, "differ") is True, dim
        assert leaves_a[path] == _field(section, "p3-brand-a"), dim
        assert leaves_b[path] == _field(section, "p3-brand-b"), dim


@pytest.fixture(scope="module")
def brand_a() -> Phase3FixtureManifest:
    return _load("p3-brand-a")


@pytest.fixture(scope="module")
def brand_b() -> Phase3FixtureManifest:
    return _load("p3-brand-b")


@pytest.fixture(scope="module")
def golden() -> dict[str, object]:
    return json.loads(GOLDEN_PATH.read_bytes())


@pytest.fixture(scope="module")
def registry() -> RegistrySnapshot:
    return registry_from_phase3_manifests(MANIFEST_DIR)


@pytest.fixture(scope="module")
def catalog(registry: RegistrySnapshot) -> tuple[str, ...]:
    return tuple(sorted(entry.asset_id for entry in registry.entries))


@pytest.fixture(scope="module")
def edit_plan(brand_a: Phase3FixtureManifest, brand_b: Phase3FixtureManifest) -> EditPlan0C:
    assert brand_a.editorial.editorial_structure_sha256 == (
        brand_b.editorial.editorial_structure_sha256
    )
    return _edit_plan(brand_a)


@pytest.fixture(scope="module")
def timeline_ir(brand_a: Phase3FixtureManifest) -> TimelineIrProduction:
    return _timeline_ir(brand_a)


@pytest.fixture(scope="module")
def profile_a(
    brand_a: Phase3FixtureManifest, registry: RegistrySnapshot, catalog: tuple[str, ...]
) -> ResolvedPresentationProfile:
    return resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        registry=registry,
    )


@pytest.fixture(scope="module")
def profile_b(
    brand_a: Phase3FixtureManifest,
    brand_b: Phase3FixtureManifest,
    registry: RegistrySnapshot,
    catalog: tuple[str, ...],
) -> ResolvedPresentationProfile:
    return resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        channel=_channel(brand_b),
        registry=registry,
    )


def _compile(
    edit_plan: EditPlan0C,
    timeline_ir: TimelineIrProduction,
    profile: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> PresentationManifest:
    snapshot = freeze_job_presentation(
        profile,
        registry,
        job_date=JOB_DATE,
        job_territory=JOB_TERRITORY,
        assets_root=ASSETS_ROOT,
    )
    return compile_presentation_manifest(
        edit_plan,
        timeline_ir,
        profile,
        registry,
        job_snapshot=snapshot,
        assets_root=ASSETS_ROOT,
    )


@pytest.fixture(scope="module")
def manifest_a(
    edit_plan: EditPlan0C,
    timeline_ir: TimelineIrProduction,
    profile_a: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> PresentationManifest:
    return _compile(edit_plan, timeline_ir, profile_a, registry)


@pytest.fixture(scope="module")
def manifest_b(
    edit_plan: EditPlan0C,
    timeline_ir: TimelineIrProduction,
    profile_b: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
) -> PresentationManifest:
    return _compile(edit_plan, timeline_ir, profile_b, registry)


def test_double_compile_is_byte_identical(
    edit_plan: EditPlan0C,
    timeline_ir: TimelineIrProduction,
    profile_a: ResolvedPresentationProfile,
    registry: RegistrySnapshot,
    manifest_a: PresentationManifest,
) -> None:
    again = _compile(edit_plan, timeline_ir, profile_a, registry)
    assert again.canonical_bytes() == manifest_a.canonical_bytes()
    assert again.manifest_sha256 == manifest_a.manifest_sha256


def test_ab_same_ir_manifests_differ_only_in_declared_dimensions(
    manifest_a: PresentationManifest, manifest_b: PresentationManifest, golden: dict[str, object]
) -> None:
    _assert_matches_golden(
        manifest_a.model_dump(mode="json"), manifest_b.model_dump(mode="json"), golden
    )


def test_ab_editorial_and_invariant_fields_are_equal(
    manifest_a: PresentationManifest,
    manifest_b: PresentationManifest,
    golden: dict[str, object],
) -> None:
    invariants = cast("dict[str, object]", golden["invariants"])
    payload_a = manifest_a.model_dump(mode="json")
    payload_b = manifest_b.model_dump(mode="json")

    assert manifest_a.editorial_fingerprint == manifest_b.editorial_fingerprint
    assert payload_a["editorial"] == payload_b["editorial"]
    assert payload_a["placement"] == payload_b["placement"]
    assert payload_a["registry_snapshot_sha256"] == payload_b["registry_snapshot_sha256"]

    style_invariants = cast("dict[str, object]", invariants["style_invariants"])
    for key, value in style_invariants.items():
        assert payload_a["style"][key] == value == payload_b["style"][key]

    audio_invariants = cast("dict[str, object]", invariants["audio_invariants"])
    for key, value in audio_invariants.items():
        assert payload_a["audio"][key] == value == payload_b["audio"][key]

    asset_kinds = cast("list[str]", invariants["asset_kinds"])
    assert sorted(manifest_a.assets) == sorted(asset_kinds)
    assert sorted(manifest_b.assets) == sorted(asset_kinds)


def test_approved_generated_assets_resolve_and_compile(
    manifest_a: PresentationManifest, golden: dict[str, object]
) -> None:
    assert manifest_a.verify_hash() is True
    expected = cast(
        "dict[str, str]",
        _field(_field(_field(golden, "fixtures"), "p3-brand-a"), "assets"),
    )
    assert set(manifest_a.assets) == set(expected)
    for kind, ref in manifest_a.assets.items():
        assert ref.sha256 == expected[kind]


def test_changed_asset_after_snapshot_blocks(
    brand_a: Phase3FixtureManifest,
    edit_plan: EditPlan0C,
    timeline_ir: TimelineIrProduction,
    tmp_path: Path,
) -> None:
    copied_root = tmp_path / "phase-3"
    shutil.copytree(MANIFEST_DIR, copied_root)

    local_registry = registry_from_phase3_manifests(copied_root)
    catalog = tuple(sorted(entry.asset_id for entry in local_registry.entries))
    profile = resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        registry=local_registry,
    )
    snapshot = freeze_job_presentation(
        profile,
        local_registry,
        job_date=JOB_DATE,
        job_territory=JOB_TERRITORY,
        assets_root=tmp_path,
    )

    target = copied_root / "assets" / "p3-brand-a" / "intro.mov"
    target.write_bytes(target.read_bytes() + b"\x00")

    with pytest.raises(StaleSnapshotError, match="asset bytes"):
        verify_job_presentation(snapshot, profile, local_registry, tmp_path)
    with pytest.raises(StaleSnapshotError, match="asset bytes"):
        compile_presentation_manifest(
            edit_plan,
            timeline_ir,
            profile,
            local_registry,
            job_snapshot=snapshot,
            assets_root=tmp_path,
        )


def test_profile_drift_after_snapshot_blocks(
    brand_a: Phase3FixtureManifest,
    brand_b: Phase3FixtureManifest,
    registry: RegistrySnapshot,
    catalog: tuple[str, ...],
) -> None:
    profile = resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        registry=registry,
    )
    snapshot = freeze_job_presentation(
        profile,
        registry,
        job_date=JOB_DATE,
        job_territory=JOB_TERRITORY,
        assets_root=ASSETS_ROOT,
    )
    drifted = resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        channel=_channel(brand_b),
        registry=registry,
    )

    with pytest.raises(StaleSnapshotError, match="profile"):
        verify_job_presentation(snapshot, drifted, registry, ASSETS_ROOT)


def test_registry_drift_after_snapshot_blocks(
    brand_a: Phase3FixtureManifest, registry: RegistrySnapshot, catalog: tuple[str, ...]
) -> None:
    profile = resolve_presentation_profile(
        _system(brand_a, catalog),
        episode=EpisodePresentationProfile(episode_id="episode-p3"),
        registry=registry,
    )
    snapshot = freeze_job_presentation(
        profile,
        registry,
        job_date=JOB_DATE,
        job_territory=JOB_TERRITORY,
        assets_root=ASSETS_ROOT,
    )
    drifted = register_assets(*registry.entries, _extra_entry())

    with pytest.raises(StaleSnapshotError, match="registry"):
        verify_job_presentation(snapshot, profile, drifted, ASSETS_ROOT)


def test_mutated_golden_expectations_are_refused(
    manifest_a: PresentationManifest, manifest_b: PresentationManifest, golden: dict[str, object]
) -> None:
    payload_a = manifest_a.model_dump(mode="json")
    payload_b = manifest_b.model_dump(mode="json")

    phantom_dimension = copy.deepcopy(golden)
    phantom_ab = cast("dict[str, object]", phantom_dimension["ab_diff"])
    phantom_ab["declared_dimensions"] = [
        *cast(
            "list[str]",
            _field(_field(golden, "ab_diff"), "declared_dimensions"),
        ),
        "style.font_family",
    ]
    with pytest.raises(AssertionError):
        _assert_matches_golden(payload_a, payload_b, phantom_dimension)

    mutated_value = copy.deepcopy(golden)
    mutated_style = cast(
        "dict[str, object]", _field(_field(mutated_value, "ab_diff"), "style")
    )
    mutated_style["font_size_px"] = {
        "differ": True,
        "p3-brand-a": 4242,
        "p3-brand-b": 4242,
    }
    with pytest.raises(AssertionError):
        _assert_matches_golden(payload_a, payload_b, mutated_value)
