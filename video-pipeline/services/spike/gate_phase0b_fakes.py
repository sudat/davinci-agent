"""Offline synthesis of a complete Phase-0B gate evidence tree from fakes.

Builds the same per-variant layout the live driver produces (source manifest,
normalize record, conform map, live readback report) using the synthetic
conform worlds — no media, no Resolve — so the evaluator's rejection paths
(off-by-one golden, missing duplicate report, >1-frame sync, stale Resolve
build) are demonstrable offline. Every synthesized accounting table is
computed through the frozen conform model, never hand-typed; tampered records
are re-sealed so pure hash checks pass and only semantic recompute catches
them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.conform.map_build import build_conform_map
from services.conform.map_synthetic import SyntheticWorld, synthetic_world
from services.contracts.primitives import RationalFrameRate
from services.contracts.serialization import canonical_json_bytes
from services.fixtures.manifest_phase0b import Phase0BFixtureManifest
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.gates.phase0b import PHASE_0B_VARIANTS
from services.normalize.models import NormalizeRecord, seal_normalize_record
from services.resolve_bridge.build_report_fakes import synthetic_host_report
from services.resolve_bridge.fixed_presentation_models import FRAME_ORIGIN
from services.spike.gate_phase0b_models import (
    CONFORM_MAP_NAME,
    INGEST_DIR,
    MAP_DIR,
    NORMALIZE_DIR,
    NORMALIZE_RECORD_NAME,
    READBACK_DIR,
    READBACK_REPORT_NAME,
    SOURCE_MANIFEST_NAME,
    LiveReadbackReport,
    ReadbackBindings,
    ReadbackItem,
    ReadbackObservation,
    SyncRow,
    fixture_manifest_path,
)
from services.spike.gate_phase0b_readback import (
    ReadbackRefusedError,
    build_placements,
    require_edit_source,
)
from services.spike.gate_phase0b_sync import measure_variant

if TYPE_CHECKING:
    from services.conform.map_models import MapFacts

HOST_NAME: Final = "resolve-host.json"
SYNC_DRIFT_SAMPLES: Final = 2000


@dataclass(frozen=True, slots=True)
class FaultKnobs0b:
    off_by_one_golden: bool = False
    missing_duplicate_report: bool = False
    sync_over_one_frame: bool = False
    stale_resolve_build: bool = False


def load_world(fixture: Phase0BFixtureManifest) -> SyntheticWorld:
    table = fixture.rational_frame_rate_table
    return synthetic_world(
        rate=RationalFrameRate(num=table.frame_rate.num, den=table.frame_rate.den),
        frames=fixture.conversions["cfr30"].input_frames,
        suffix=fixture.fixture_id.removeprefix("p0b-"),
    )


def tamper_record(
    record: NormalizeRecord, *, output_delta: int = 0, drop_first_duplicate: bool = False
) -> NormalizeRecord:
    expected = record.drop_dup.expected
    duplicated = expected.duplicated[1:] if drop_first_duplicate else expected.duplicated
    updated = record.drop_dup.model_copy(
        update={
            "expected": expected.model_copy(
                update={
                    "output_frames": expected.output_frames + output_delta,
                    "duplicated": duplicated,
                }
            )
        }
    )
    return seal_normalize_record(
        record.model_copy(
            update={
                "drop_dup": updated,
                "output_semantics": record.output_semantics.model_copy(
                    update={
                        "observed_output_frames": (
                            record.output_semantics.observed_output_frames + output_delta
                        )
                    }
                ),
            },
        )
    )


def drifted_facts(world: SyntheticWorld) -> MapFacts:
    audio = replace(
        world.facts.edit_audio, start_offset_samples=SYNC_DRIFT_SAMPLES
    )
    return replace(world.facts, edit_audio=audio)


def fake_readback(
    fixture: Phase0BFixtureManifest,
    fixture_sha: str,
    host_sha: str,
    version: str,
    build: str,
    world: SyntheticWorld,
    written_record: NormalizeRecord,
    golden_output_frames: int,
) -> LiveReadbackReport:
    placements = build_placements(fixture, golden_output_frames)
    items = tuple(
        ReadbackItem(
            requested=placement,
            observed=ReadbackObservation(
                source_start=placement.source_start,
                source_end=placement.source_end,
                record_start=placement.record_start,
                record_end=placement.record_start + (placement.source_end - placement.source_start),
                media_path=world.record.output.path,
            ),
        )
        for placement in placements
    )
    return LiveReadbackReport(
        schema_version="phase-0b-readback-v1",
        fixture_id=fixture.fixture_id,
        bindings=ReadbackBindings(
            host_report_sha256=host_sha,
            fixture_manifest_sha256=fixture_sha,
            source_manifest_sha256=world.manifest.content_hash,
            normalize_record_sha256=written_record.content_hash,
            edit_source_sha256=written_record.output.sha256,
            resolve_version=version,
            resolve_build=build,
        ),
        timeline_rate_num=30,
        timeline_rate_den=1,
        frame_origin=FRAME_ORIGIN,
        items=items,
        clip_properties=(),
        project_name="__fvp_test__fake",
    )


def synthesize0b(evidence: Path, knobs: FaultKnobs0b) -> Path:
    host = synthetic_host_report()
    atomic_write(evidence / HOST_NAME, canonical_model_bytes(host))
    host_sha = sha256_file(evidence / HOST_NAME)
    for variant in PHASE_0B_VARIANTS:
        fixture_path = fixture_manifest_path(variant)
        fixture = Phase0BFixtureManifest.model_validate_json(fixture_path.read_bytes())
        world = load_world(fixture)
        sealed = seal_normalize_record(world.record)
        world = SyntheticWorld(
            manifest=world.manifest, record=sealed, facts=world.facts
        )
        written = sealed
        if knobs.off_by_one_golden and variant == "p0b-cfr24":
            written = tamper_record(world.record, output_delta=1)
        if knobs.missing_duplicate_report and variant == "p0b-vfr-2-3-cadence":
            written = tamper_record(world.record, drop_first_duplicate=True)
        facts = (
            drifted_facts(world)
            if knobs.sync_over_one_frame and variant == "p0b-audio-offset1024"
            else world.facts
        )
        conform_map = build_conform_map(
            world.manifest,
            world.record,
            facts,
            audio_content_offset_samples=fixture.generation.audio.content_offset_samples,
        )
        stale = knobs.stale_resolve_build and variant == "p0b-rotate90"
        report = fake_readback(
            fixture,
            sha256_file(fixture_path),
            "f" * 64 if stale else host_sha,
            host.application.version,
            host.application.build,
            world,
            written,
            world.record.drop_dup.expected.output_frames,
        )
        run = evidence / "runs" / variant
        for sub in (INGEST_DIR, NORMALIZE_DIR, MAP_DIR, READBACK_DIR):
            (run / sub).mkdir(parents=True, exist_ok=True)
        atomic_write(run / INGEST_DIR / SOURCE_MANIFEST_NAME, canonical_model_bytes(world.manifest))
        atomic_write(run / NORMALIZE_DIR / NORMALIZE_RECORD_NAME, canonical_model_bytes(written))
        atomic_write(run / MAP_DIR / CONFORM_MAP_NAME, canonical_json_bytes(conform_map))
        atomic_write(run / READBACK_DIR / READBACK_REPORT_NAME, canonical_model_bytes(report))
    return evidence / HOST_NAME


def happy_sync_rows(variant: str) -> tuple[SyncRow, ...]:
    fixture = Phase0BFixtureManifest.model_validate_json(
        fixture_manifest_path(variant).read_bytes()
    )
    world = load_world(fixture)
    conform_map = build_conform_map(
        world.manifest,
        world.record,
        world.facts,
        audio_content_offset_samples=fixture.generation.audio.content_offset_samples,
    )
    return measure_variant(variant, fixture, conform_map)


def refusal_label(record: NormalizeRecord, original_path: Path) -> str:
    """Probe the edit-source guard with an original; return the refusal label."""

    try:
        require_edit_source(original_path, record)
    except ReadbackRefusedError as error:
        return error.label
    raise AssertionError("guard unexpectedly accepted an original for a Resolve edit")
