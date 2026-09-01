"""Conform Map artifact tests: build, validate, and query (todo-23).

Happy paths run the real frozen chain (fixture -> ingest -> normalize -> map)
for four Phase-0B variants; failure paths use the synthetic worlds from
``services.conform.map_faults`` so rejections never depend on media.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from services.conform import map_cli
from services.conform.coordinates import OriginalTimestamp, PtsSpan
from services.conform.errors import CoordinateRangeError
from services.conform.map_build import ConformMapBuildError, build_conform_map
from services.conform.map_faults import (
    RATE_24,
    corrupt_map_bytes,
    craft_gap_map,
    craft_identity_mismatch,
    craft_non_monotonic_map,
    craft_unreported_duplicate_map,
    synthetic_world,
)
from services.conform.map_models import (
    AudioAffineMap,
    ConformMap,
    compute_table_sha256,
    seal_conform_map,
    verify_conform_map_hash,
)
from services.conform.map_probe import probe_map_facts
from services.conform.map_query import (
    edit_frame_to_original_pts,
    edit_sample_to_original_sample,
    original_pts_to_edit_frame,
    original_sample_to_edit_sample,
    original_span_to_edit_span,
)
from services.conform.map_validate import MapValidationError, validate_conform_map
from services.contracts.serialization import canonical_json_bytes
from services.foundation_io import sha256_file
from services.ingest.fixture_inputs import materialize_fixture
from services.ingest.ingest import register_one
from services.normalize.cli_support import lock_recipe_pointer
from services.normalize.runner import NormalizeContext, normalize_one
from services.toolchain.models import Phase0BToolchainLock, load_lock

if TYPE_CHECKING:
    from services.conform.map_build import MapFacts
    from services.ingest.models import SourceManifest
    from services.normalize.models import NormalizeRecord
    from services.toolchain.models import AnyToolchainLock

PHASE_0B_LOCK = Path("config/toolchains/phase-0b-v2.json")
MANIFEST_DIR = Path("tests/fixtures/manifests/phase-0b")
MEDIA_FIXTURES = (
    "p0b-rotate90",
    "p0b-cfr24",
    "p0b-vfr-2-3-cadence",
    "p0b-audio-offset1024",
)


@dataclass(frozen=True, slots=True)
class MapBundle:
    manifest: SourceManifest
    record: NormalizeRecord
    facts: MapFacts
    conform_map: ConformMap


def _pinned(name: str, env_key: str, lock: AnyToolchainLock) -> Path:
    override = os.environ.get(env_key)
    path = Path(override) if override else Path(getattr(lock.ffmpeg, name).path)
    if not path.is_file():
        pytest.skip(f"pinned {name} not bootstrapped: {path}")
    return path


@pytest.fixture(scope="session")
def phase0b_lock() -> Phase0BToolchainLock:
    lock = load_lock(PHASE_0B_LOCK)
    assert isinstance(lock, Phase0BToolchainLock)
    return lock


@pytest.fixture(scope="session")
def pinned_ffmpeg(phase0b_lock: Phase0BToolchainLock) -> Path:
    return _pinned("ffmpeg", "FVP_FFMPEG_BIN", phase0b_lock)


@pytest.fixture(scope="session")
def pinned_ffprobe(phase0b_lock: Phase0BToolchainLock) -> Path:
    return _pinned("ffprobe", "FVP_FFPROBE_BIN", phase0b_lock)


@pytest.fixture(scope="session")
def built_maps(
    pinned_ffmpeg: Path, pinned_ffprobe: Path, tmp_path_factory: pytest.TempPathFactory
) -> Mapping[str, MapBundle]:
    root = tmp_path_factory.mktemp("todo23-maps")
    bundles: dict[str, MapBundle] = {}
    for fixture_id in MEDIA_FIXTURES:
        media = materialize_fixture(
            MANIFEST_DIR / f"{fixture_id}.json", pinned_ffmpeg, root / "media"
        )
        manifest = register_one(
            original=media,
            ffprobe=pinned_ffprobe,
            recipe=lock_recipe_pointer(PHASE_0B_LOCK, fixture_id),
            out=root / "manifests" / f"{fixture_id}.source-manifest.json",
        )
        record = normalize_one(
            manifest,
            "cfr30",
            NormalizeContext(
                lock_path=PHASE_0B_LOCK,
                ffmpeg=pinned_ffmpeg,
                ffprobe=pinned_ffprobe,
                output_dir=root / "edit-sources",
                record_out=root / "records" / f"{fixture_id}.normalize-record.json",
            ),
        )
        facts = probe_map_facts(
            pinned_ffprobe, manifest=manifest, record=record
        )
        offset = 1024 if fixture_id == "p0b-audio-offset1024" else 0
        conform_map = build_conform_map(
            manifest, record, facts, audio_content_offset_samples=offset
        )
        validate_conform_map(
            conform_map, source_manifest=manifest, normalize_record=record
        )
        bundles[fixture_id] = MapBundle(manifest, record, facts, conform_map)
    return bundles


def _assert_float_free(node: object) -> None:
    assert not isinstance(node, float)
    if isinstance(node, dict):
        for value in node.values():
            _assert_float_free(value)
    elif isinstance(node, list):
        for value in node:
            _assert_float_free(value)


def _golden_conversion(fixture_id: str) -> dict[str, object]:
    payload = json.loads((MANIFEST_DIR / f"{fixture_id}.json").read_bytes())
    golden = payload["conversions"]["cfr30"]
    assert isinstance(golden, dict)
    return golden


def test_synthetic_identity_world_builds_one_to_one_table() -> None:
    world = synthetic_world()
    conform_map = build_conform_map(world.manifest, world.record, world.facts)
    validate_conform_map(
        conform_map, source_manifest=world.manifest, normalize_record=world.record
    )
    rows = conform_map.video_table.rows
    assert len(rows) == 10
    assert [row.edit_frame for row in rows] == list(range(10))
    assert [row.source_frame for row in rows] == list(range(10))
    assert all(row.original_pts.pts == row.source_frame * 1000 for row in rows)
    assert all(not row.duplicate for row in rows)
    assert conform_map.normalization.dropped_source_frames == ()
    assert conform_map.normalization.duplicated_source_frames == ()


def test_identity_map_cfr30_source_with_rotation(
    built_maps: Mapping[str, MapBundle],
) -> None:
    bundle = built_maps["p0b-rotate90"]
    rows = bundle.conform_map.video_table.rows
    assert len(rows) == 600
    assert [row.source_frame for row in rows] == list(range(600))
    assert [row.original_pts.pts for row in rows] == [frame * 1000 for frame in range(600)]
    assert all(not row.duplicate for row in rows)
    assert bundle.conform_map.normalization.rotation_degrees == 90
    assert (
        bundle.conform_map.normalization.rotation_policy
        == bundle.record.policy.rotation
    )
    audio = bundle.conform_map.audio_map
    assert isinstance(audio, AudioAffineMap)
    assert audio.rate_equal is True
    assert audio.origin_original_sample == 0
    assert audio.origin_edit_sample == 0
    assert audio.original_sample_count == 960000
    assert audio.edit_sample_count == 960000


def test_cfr24_duplicate_table_matches_frozen_golden(
    built_maps: Mapping[str, MapBundle],
) -> None:
    golden = _golden_conversion("p0b-cfr24")
    rows = built_maps["p0b-cfr24"].conform_map.video_table.rows
    assert len(rows) == golden["output_frames"] == 750
    flagged = [row.source_frame for row in rows if row.duplicate]
    assert flagged == golden["duplicated_source_frames"] == [
        index for index in range(600) if index % 4 == 1
    ]
    assert rows[1].source_frame == 1
    assert rows[2].source_frame == 1
    assert rows[2].original_pts == rows[1].original_pts
    assert rows[3].source_frame == 2


def test_vfr_table_matches_frozen_golden(
    built_maps: Mapping[str, MapBundle],
) -> None:
    golden = _golden_conversion("p0b-vfr-2-3-cadence")
    table = built_maps["p0b-vfr-2-3-cadence"].conform_map.video_table
    assert table.source_frame_count == 120
    assert (table.source_rate.num, table.source_rate.den) == (24, 1)
    assert len(table.rows) == golden["output_frames"] == 150
    flagged = [row.source_frame for row in table.rows if row.duplicate]
    assert flagged == golden["duplicated_source_frames"] == [
        index for index in range(120) if index % 4 == 1
    ]
    assert table.rows[2].source_frame == 1
    assert table.rows[2].duplicate is True
    assert table.rows[3].source_frame == 2


def test_audio_offset_map_records_explicit_origin_shift(
    built_maps: Mapping[str, MapBundle],
) -> None:
    offset_bundle = built_maps["p0b-audio-offset1024"]
    assert offset_bundle.conform_map.normalization.audio_content_offset_samples == 1024
    audio = offset_bundle.conform_map.audio_map
    assert isinstance(audio, AudioAffineMap)
    assert audio.rate_equal is True
    assert audio.original_sample_count == 960000
    assert original_sample_to_edit_sample(offset_bundle.conform_map, 241024) == 241024
    assert edit_sample_to_original_sample(offset_bundle.conform_map, 721024) == 721024
    for fixture_id in ("p0b-rotate90", "p0b-cfr24", "p0b-vfr-2-3-cadence"):
        other = built_maps[fixture_id].conform_map
        assert other.normalization.audio_content_offset_samples == 0


def test_map_is_float_free_strict_and_sealed(
    built_maps: Mapping[str, MapBundle],
) -> None:
    conform_map = built_maps["p0b-cfr24"].conform_map
    payload = json.loads(canonical_json_bytes(conform_map))
    _assert_float_free(payload)
    assert verify_conform_map_hash(conform_map)
    mutated = json.loads(canonical_json_bytes(conform_map))
    mutated["surprise"] = 1
    with pytest.raises(ValidationError):
        ConformMap.model_validate(json.dumps(mutated))
    mutated_video = json.loads(canonical_json_bytes(conform_map))
    mutated_video["video_table"]["rows"][0]["edit_frame"] = 1.5
    with pytest.raises(ValidationError):
        ConformMap.model_validate(json.dumps(mutated_video))


def test_deterministic_hash_and_replay_identical_bytes(
    built_maps: Mapping[str, MapBundle],
) -> None:
    bundle = built_maps["p0b-vfr-2-3-cadence"]
    replay = build_conform_map(
        bundle.manifest,
        bundle.record,
        bundle.facts,
        audio_content_offset_samples=0,
    )
    assert canonical_json_bytes(replay) == canonical_json_bytes(bundle.conform_map)
    assert replay.table_sha256 == bundle.conform_map.table_sha256
    assert (
        compute_table_sha256(replay.video_table, replay.audio_map)
        == replay.table_sha256
    )
    assert seal_conform_map(replay).content_hash == bundle.conform_map.content_hash


def test_build_rejects_record_accounting_mismatch() -> None:
    world = synthetic_world(target=RATE_24)
    expected = world.record.drop_dup.expected
    drifted = world.record.model_copy(
        update={
            "drop_dup": world.record.drop_dup.model_copy(
                update={
                    "expected": expected.model_copy(
                        update={"output_frames": expected.output_frames + 1}
                    )
                }
            )
        }
    )
    with pytest.raises(ConformMapBuildError) as raised:
        build_conform_map(world.manifest, drifted, world.facts)
    assert raised.value.reason_code == "record_accounting_mismatch"


def test_build_rejects_wrong_source_identity() -> None:
    first = synthetic_world()
    second = synthetic_world(rate=RATE_24, tb_den=24000, suffix="b")
    with pytest.raises(ConformMapBuildError) as raised:
        build_conform_map(first.manifest, second.record, second.facts)
    assert raised.value.reason_code == "identity_mismatch"


@pytest.mark.parametrize(
    ("crafter", "reason", "structurally_broken"),
    [
        (craft_gap_map, "coverage_gap", True),
        (craft_unreported_duplicate_map, "unreported_duplicate", True),
        (craft_non_monotonic_map, "non_monotonic_table", True),
        (craft_identity_mismatch, "identity_mismatch", False),
    ],
)
def test_validate_rejects_crafted_maps(
    crafter: Callable[[], tuple[ConformMap, SourceManifest, NormalizeRecord]],
    reason: str,
    *,
    structurally_broken: bool,
) -> None:
    conform_map, manifest, record = crafter()
    with pytest.raises(MapValidationError) as raised:
        validate_conform_map(conform_map, source_manifest=manifest, normalize_record=record)
    assert raised.value.reason_code == reason
    if structurally_broken:
        with pytest.raises(MapValidationError):
            validate_conform_map(conform_map)


def test_corrupt_map_bytes_rejected() -> None:
    with pytest.raises(ValidationError):
        ConformMap.model_validate_json(corrupt_map_bytes())


def test_validate_recomputes_hash_instead_of_trusting_bytes() -> None:
    world = synthetic_world()
    conform_map = build_conform_map(world.manifest, world.record, world.facts)
    tampered = conform_map.model_copy(
        update={"table_sha256": "0" * 64}
    )
    with pytest.raises(MapValidationError) as raised:
        validate_conform_map(tampered)
    assert raised.value.reason_code == "table_hash_mismatch"


def test_half_open_span_queries(built_maps: Mapping[str, MapBundle]) -> None:
    conform_map = built_maps["p0b-rotate90"].conform_map
    time_base = conform_map.video_table.rows[0].original_pts.time_base
    full = original_span_to_edit_span(
        conform_map, PtsSpan(start_pts=0, end_pts=600000, time_base=time_base)
    )
    assert (full.start_frame, full.end_frame) == (0, 600)
    interior = original_span_to_edit_span(
        conform_map, PtsSpan(start_pts=2000, end_pts=5000, time_base=time_base)
    )
    assert (interior.start_frame, interior.end_frame) == (2, 5)
    empty = original_span_to_edit_span(
        conform_map, PtsSpan(start_pts=2000, end_pts=2000, time_base=time_base)
    )
    assert empty.length == 0


def test_drop_aware_span_raises_and_reports() -> None:
    world = synthetic_world(target=RATE_24)
    conform_map = build_conform_map(world.manifest, world.record, world.facts)
    time_base = conform_map.video_table.rows[0].original_pts.time_base
    assert conform_map.normalization.dropped_source_frames == (2, 7)
    assert [row.source_frame for row in conform_map.video_table.rows] == [
        0,
        1,
        3,
        4,
        5,
        6,
        8,
        9,
    ]
    with pytest.raises(CoordinateRangeError) as raised:
        original_span_to_edit_span(
            conform_map, PtsSpan(start_pts=2000, end_pts=3000, time_base=time_base)
        )
    assert "dropped" in str(raised.value)
    clean = original_span_to_edit_span(
        conform_map, PtsSpan(start_pts=3000, end_pts=5000, time_base=time_base)
    )
    assert (clean.start_frame, clean.end_frame) == (2, 4)


def test_pts_frame_round_trip_and_out_of_range(
    built_maps: Mapping[str, MapBundle],
) -> None:
    conform_map = built_maps["p0b-rotate90"].conform_map
    time_base = conform_map.video_table.rows[0].original_pts.time_base
    for frame in (0, 1, 299, 599):
        timestamp = edit_frame_to_original_pts(conform_map, frame)
        assert timestamp.time_base == time_base
        assert original_pts_to_edit_frame(conform_map, timestamp) == frame
    with pytest.raises(CoordinateRangeError):
        edit_frame_to_original_pts(conform_map, 600)
    with pytest.raises(CoordinateRangeError):
        edit_frame_to_original_pts(conform_map, 999999)
    with pytest.raises(CoordinateRangeError):
        original_pts_to_edit_frame(
            conform_map,
            OriginalTimestamp(pts=600000, time_base=time_base),
        )


def test_dropped_frame_point_query_raises() -> None:
    world = synthetic_world(target=RATE_24)
    conform_map = build_conform_map(world.manifest, world.record, world.facts)
    time_base = conform_map.video_table.rows[0].original_pts.time_base
    with pytest.raises(CoordinateRangeError) as raised:
        original_pts_to_edit_frame(
            conform_map, OriginalTimestamp(pts=2000, time_base=time_base)
        )
    assert "dropped" in str(raised.value)


def test_audio_sample_queries_and_bounds(
    built_maps: Mapping[str, MapBundle],
) -> None:
    conform_map = built_maps["p0b-audio-offset1024"].conform_map
    for sample in (0, 1, 1024, 241024, 959999):
        assert original_sample_to_edit_sample(conform_map, sample) == sample
        assert edit_sample_to_original_sample(conform_map, sample) == sample
    with pytest.raises(CoordinateRangeError):
        original_sample_to_edit_sample(conform_map, 960000)
    with pytest.raises(CoordinateRangeError):
        edit_sample_to_original_sample(conform_map, 960000)


def test_cli_fault_scenarios_exit_2(tmp_path: Path) -> None:
    for scenario in (
        "video-table-gap",
        "unreported-duplicate",
        "non-monotonic-table",
        "wrong-source-id",
        "out-of-range-query",
        "corrupt-map-bytes",
    ):
        crafted = tmp_path / f"{scenario}.conform-map.json"
        assert (
            map_cli.main(
                ["fault", "--scenario", scenario, "--write", str(crafted)]
            )
            == 2
        )


def test_cli_query_round_trip(tmp_path: Path) -> None:
    world = synthetic_world()
    conform_map = build_conform_map(world.manifest, world.record, world.facts)
    map_path = tmp_path / "identity.conform-map.json"
    map_path.write_bytes(canonical_json_bytes(conform_map))
    assert (
        map_cli.main(
            [
                "query",
                "--map",
                str(map_path),
                "--mode",
                "frame-to-pts",
                "--frame",
                "3",
            ]
        )
        == 0
    )
    assert (
        map_cli.main(
            [
                "query",
                "--map",
                str(map_path),
                "--mode",
                "frame-to-pts",
                "--frame",
                "999999",
            ]
        )
        == 2
    )


def test_map_bound_to_real_chain_identities(
    built_maps: Mapping[str, MapBundle],
) -> None:
    bundle = built_maps["p0b-cfr24"]
    conform_map = bundle.conform_map
    assert conform_map.original.source_id == bundle.manifest.artifact_id
    assert conform_map.original.sha256 == bundle.manifest.file.sha256
    assert conform_map.original.sha256 == sha256_file(Path(bundle.manifest.file.path))
    assert conform_map.edit_source.sha256 == bundle.record.output.sha256
    assert conform_map.edit_source.normalize_record_content_hash == (
        bundle.record.content_hash
    )
    assert conform_map.normalization.recipe_id == (
        bundle.manifest.edit_source_recipe.recipe_id
    )
    assert conform_map.normalization.output_frames == (
        bundle.record.drop_dup.expected.output_frames
    )
