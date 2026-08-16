"""Live driver for the Phase-0B gate evidence tree.

One Resolve session for the whole gate. For each of the six frozen variants:
materialize the frozen recipe, register the SourceManifest, normalize to the
CFR30 Edit Mezzanine, build+validate the ConformMap, and drive the live
readback. Raw evidence only — pass/fail is recomputed separately by
:mod:`services.spike.gate_phase0b_evaluate`. Owned ``__fvp_test__`` projects
are cleaned up after every readback and at gate end; Resolve is left running.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from services.conform.map_build import ConformMapBuildError, build_conform_map
from services.conform.map_probe import probe_map_facts
from services.conform.map_validate import MapValidationError, validate_conform_map
from services.contracts.serialization import canonical_json_bytes
from services.fixtures.manifest_phase0b import Phase0BFixtureManifest
from services.foundation_io import atomic_write, sha256_file
from services.gates.phase0b import PHASE_0B_VARIANTS
from services.ingest.fixture_inputs import materialize_fixture
from services.ingest.ingest import register_one
from services.normalize.cli_support import lock_recipe_pointer
from services.normalize.errors import NormalizeError
from services.normalize.runner import NormalizeContext, normalize_one
from services.resolve_bridge.base_cut_plan import BaseCutError
from services.resolve_bridge.connection import BridgeConnectionError, connect
from services.resolve_bridge.lifecycle import cleanup_owned_projects
from services.resolve_bridge.readiness import load_host_report
from services.spike.gate_phase0b_models import (
    CONFORM_MAP_NAME,
    EDIT_SOURCES_DIR,
    GOLDEN_DIR,
    INGEST_DIR,
    NORMALIZE_DIR,
    NORMALIZE_RECORD_NAME,
    READBACK_DIR,
    READBACK_TIMEOUT_SECONDS,
    SOURCE_MANIFEST_NAME,
    fixture_manifest_path,
)
from services.spike.gate_phase0b_readback import ReadbackRefusedError, live_readback

if TYPE_CHECKING:
    from services.conform.map_models import ConformMap
    from services.ingest.models import SourceManifest
    from services.normalize.models import NormalizeRecord


class Gate0bBindingError(Exception):
    """The policy does not bind the current frozen inputs."""


class Gate0bDriverError(Exception):
    """The evidence tree could not be completed within its bounds."""


class Gate0bUnavailableError(Exception):
    """The live Resolve bridge could not be reached."""


@dataclass(frozen=True, slots=True)
class Drive0bResult:
    variants: tuple[str, ...]


def preflight_policy_bindings(lock_path: Path, policy_refs: PolicyRefs) -> None:
    combined = hashlib.sha256()
    for variant in PHASE_0B_VARIANTS:
        combined.update(fixture_manifest_path(variant).read_bytes())
    if combined.hexdigest() != policy_refs.fixture_manifest_sha256:
        raise Gate0bBindingError("combined fixture-manifest hash drift vs policy")
    if sha256_file(lock_path) != policy_refs.toolchain_lock_sha256:
        raise Gate0bBindingError("toolchain lock hash drift vs policy")
    if sha256_file(GOLDEN_DIR / "index.json") != policy_refs.golden_sha256:
        raise Gate0bBindingError("golden index hash drift vs policy")


@dataclass(frozen=True, slots=True)
class PolicyRefs:
    fixture_manifest_sha256: str
    toolchain_lock_sha256: str
    golden_sha256: str


def _ingest(
    variant: str, ffmpeg: Path, ffprobe: Path, lock_path: Path, out_dir: Path
) -> SourceManifest:
    media = materialize_fixture(fixture_manifest_path(variant), ffmpeg, out_dir / "media")
    return register_one(
        original=media,
        ffprobe=ffprobe,
        recipe=lock_recipe_pointer(lock_path, variant),
        out=out_dir / SOURCE_MANIFEST_NAME,
    )


def _normalize(
    manifest: SourceManifest, lock_path: Path, ffmpeg: Path, ffprobe: Path, out_dir: Path
) -> NormalizeRecord:
    return normalize_one(
        manifest,
        "cfr30",
        NormalizeContext(
            lock_path=lock_path,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            output_dir=out_dir / EDIT_SOURCES_DIR,
            record_out=out_dir / NORMALIZE_RECORD_NAME,
        ),
    )


def _build_map(
    variant: str,
    manifest: SourceManifest,
    record: NormalizeRecord,
    ffprobe: Path,
    out_dir: Path,
) -> ConformMap:
    fixture = Phase0BFixtureManifest.model_validate_json(
        fixture_manifest_path(variant).read_bytes()
    )
    facts = probe_map_facts(ffprobe, manifest=manifest, record=record)
    conform_map = build_conform_map(
        manifest,
        record,
        facts,
        audio_content_offset_samples=fixture.generation.audio.content_offset_samples,
    )
    validate_conform_map(conform_map, source_manifest=manifest, normalize_record=record)
    replay = build_conform_map(
        manifest,
        record,
        facts,
        audio_content_offset_samples=fixture.generation.audio.content_offset_samples,
    )
    if canonical_json_bytes(replay) != canonical_json_bytes(conform_map):
        raise Gate0bDriverError(f"{variant}: conform map rebuild is not deterministic")
    atomic_write(out_dir / CONFORM_MAP_NAME, canonical_json_bytes(conform_map))
    return conform_map


def drive_phase0b(
    evidence: Path,
    host_report_path: Path,
    lock_path: Path,
    ffmpeg: Path,
    ffprobe: Path,
    budget_seconds: float,
) -> Drive0bResult:
    deadline = time.monotonic() + budget_seconds
    host = load_host_report(host_report_path)
    host_sha = sha256_file(host_report_path)
    try:
        connection = connect(host)
    except BridgeConnectionError as error:
        raise Gate0bUnavailableError(str(error)) from error
    completed: list[str] = []
    try:
        for variant in PHASE_0B_VARIANTS:
            if time.monotonic() > deadline:
                raise Gate0bDriverError(f"gate budget exhausted before {variant}")
            run_dir = evidence / "runs" / variant
            fixture = Phase0BFixtureManifest.model_validate_json(
                fixture_manifest_path(variant).read_bytes()
            )
            manifest = _ingest(variant, ffmpeg, ffprobe, lock_path, run_dir / INGEST_DIR)
            record = _normalize(
                manifest, lock_path, ffmpeg, ffprobe, run_dir / NORMALIZE_DIR
            )
            _build_map(variant, manifest, record, ffprobe, run_dir / "conform-map")
            live_readback(
                connection,
                host_sha,
                fixture_manifest_path(variant),
                fixture,
                manifest,
                record,
                run_dir / READBACK_DIR,
                timeout_seconds=READBACK_TIMEOUT_SECONDS,
            )
            completed.append(variant)
            print(f"phase-0b: variant {variant} evidence complete")
    finally:
        try:
            cleanup_owned_projects(connection.project_manager())
        except Exception as cleanup_error:  # noqa: BLE001 -- never mask the primary failure
            print(f"warning: post-drive owned cleanup failed: {cleanup_error}")
    return Drive0bResult(variants=tuple(completed))


DRIVER_ERRORS: Final = (
    Gate0bDriverError,
    Gate0bBindingError,
    NormalizeError,
    ConformMapBuildError,
    MapValidationError,
    ReadbackRefusedError,
    BaseCutError,
    ValidationError,
    OSError,
)
