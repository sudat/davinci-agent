"""Offline fault crafting for Conform Map QA (todo-23 adversarial scenarios).

Each crafter builds a valid map on a synthetic world from
``services.conform.map_synthetic``, corrupts it, and re-hashes it with
``compute_table_sha256`` so passing validation proves semantic recomputation
from table bytes, not hash trust; the CLI scenarios run the real validator
and report the machine-readable rejection reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from services.conform.errors import CoordinateError
from services.conform.map_build import build_conform_map
from services.conform.map_models import ConformMap, compute_table_sha256
from services.conform.map_query import edit_frame_to_original_pts
from services.conform.map_synthetic import RATE_24, synthetic_world
from services.conform.map_validate import MapValidationError, validate_conform_map
from services.contracts.serialization import canonical_json_bytes

if TYPE_CHECKING:
    from collections.abc import Callable

    from services.ingest.models import SourceManifest
    from services.normalize.models import NormalizeRecord

SCENARIOS = (
    "video-table-gap",
    "unreported-duplicate",
    "non-monotonic-table",
    "wrong-source-id",
    "out-of-range-query",
    "corrupt-map-bytes",
)


def _identity_world() -> tuple[ConformMap, SourceManifest, NormalizeRecord]:
    world = synthetic_world()
    conform_map = build_conform_map(world.manifest, world.record, world.facts)
    return conform_map, world.manifest, world.record


def _rehashed(conform_map: ConformMap) -> ConformMap:
    return conform_map.model_copy(
        update={
            "table_sha256": compute_table_sha256(
                conform_map.video_table, conform_map.audio_map
            )
        }
    )


def craft_gap_map() -> tuple[ConformMap, SourceManifest, NormalizeRecord]:
    world = synthetic_world(target=RATE_24, suffix="gap")
    conform_map = build_conform_map(world.manifest, world.record, world.facts)
    rows = list(conform_map.video_table.rows)
    rows.pop(4)
    table = conform_map.video_table.model_copy(update={"rows": tuple(rows)})
    broken = conform_map.model_copy(update={"video_table": table})
    return _rehashed(broken), world.manifest, world.record


def craft_unreported_duplicate_map() -> (
    tuple[ConformMap, SourceManifest, NormalizeRecord]
):
    world = synthetic_world(rate=RATE_24, tb_den=24000, suffix="dup")
    conform_map = build_conform_map(world.manifest, world.record, world.facts)
    index = next(
        position
        for position, row in enumerate(conform_map.video_table.rows)
        if row.duplicate
    )
    rows = list(conform_map.video_table.rows)
    rows[index] = rows[index].model_copy(update={"duplicate": False})
    table = conform_map.video_table.model_copy(update={"rows": tuple(rows)})
    return (
        _rehashed(conform_map.model_copy(update={"video_table": table})),
        world.manifest,
        world.record,
    )


def craft_non_monotonic_map() -> tuple[ConformMap, SourceManifest, NormalizeRecord]:
    conform_map, manifest, record = _identity_world()
    rows = list(conform_map.video_table.rows)
    first_row, second_row = rows[3], rows[4]
    rows[3] = first_row.model_copy(
        update={
            "source_frame": second_row.source_frame,
            "original_pts": second_row.original_pts,
        }
    )
    rows[4] = second_row.model_copy(
        update={
            "source_frame": first_row.source_frame,
            "original_pts": first_row.original_pts,
        }
    )
    table = conform_map.video_table.model_copy(update={"rows": tuple(rows)})
    return (
        _rehashed(conform_map.model_copy(update={"video_table": table})),
        manifest,
        record,
    )


def craft_identity_mismatch() -> tuple[ConformMap, SourceManifest, NormalizeRecord]:
    world = synthetic_world(suffix="a")
    conform_map = build_conform_map(world.manifest, world.record, world.facts)
    other = synthetic_world(suffix="b")
    return conform_map, other.manifest, other.record


def corrupt_map_bytes() -> bytes:
    conform_map, _, _ = _identity_world()
    raw = canonical_json_bytes(conform_map)
    return raw[: len(raw) // 2] + b'{"rows": [trunc'


def run_scenario(scenario: str, write: Path | None) -> int:
    handlers: dict[str, Callable[[], bool]] = {
        "video-table-gap": _scenario_table_fault(craft_gap_map, "coverage_gap"),
        "unreported-duplicate": _scenario_table_fault(
            craft_unreported_duplicate_map, "unreported_duplicate"
        ),
        "non-monotonic-table": _scenario_table_fault(
            craft_non_monotonic_map, "non_monotonic_table"
        ),
        "wrong-source-id": _scenario_table_fault(
            craft_identity_mismatch, "identity_mismatch"
        ),
        "out-of-range-query": _scenario_out_of_range,
        "corrupt-map-bytes": _scenario_corrupt_bytes,
    }
    handler = handlers.get(scenario)
    if handler is None:
        print(f"error=unknown_scenario: {scenario}")
        return 2
    if write is not None:
        _write_scenario_artifact(scenario, write)
    return 2 if handler() else 1


def _write_scenario_artifact(scenario: str, write: Path) -> None:
    write.parent.mkdir(parents=True, exist_ok=True)
    if scenario == "corrupt-map-bytes":
        write.write_bytes(corrupt_map_bytes())
    elif scenario == "out-of-range-query":
        conform_map, _, _ = _identity_world()
        write.write_bytes(canonical_json_bytes(conform_map))
    else:
        crafter = {
            "video-table-gap": craft_gap_map,
            "unreported-duplicate": craft_unreported_duplicate_map,
            "non-monotonic-table": craft_non_monotonic_map,
            "wrong-source-id": craft_identity_mismatch,
        }[scenario]
        write.write_bytes(canonical_json_bytes(crafter()[0]))


def _scenario_table_fault(
    crafter: Callable[[], tuple[ConformMap, SourceManifest, NormalizeRecord]],
    expected_reason: str,
) -> Callable[[], bool]:
    def run() -> bool:
        crafted = crafter()
        try:
            validate_conform_map(
                crafted[0], source_manifest=crafted[1], normalize_record=crafted[2]
            )
        except MapValidationError as error:
            print(f"reason={error.reason_code}")
            print(f"error=map_invalid: {error}")
            return error.reason_code == expected_reason
        print("fault was not detected")
        return False

    return run


def _scenario_out_of_range() -> bool:
    conform_map, _, _ = _identity_world()
    try:
        edit_frame_to_original_pts(conform_map, 999999)
    except CoordinateError as error:
        print("reason=coordinate_range")
        print(f"error=coordinate_range: {error}")
        return True
    print("fault was not detected")
    return False


def _scenario_corrupt_bytes() -> bool:
    try:
        ConformMap.model_validate_json(corrupt_map_bytes())
    except ValidationError as error:
        print("reason=malformed_map")
        print(f"error=malformed_map: {error.errors()[0]['type']}")
        return True
    print("fault was not detected")
    return False
