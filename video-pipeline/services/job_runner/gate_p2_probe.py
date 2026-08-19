"""Shared Phase-2 gate drive helpers: records, probes, packages, builds.

Small pure helpers the per-fixture drivers share: raw JSON record writes,
the manifest-declared fault probes through the real compiler, the fixture
media + live-package materialization, and the single-writer live build.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.build.builder_models import LeaseHeld
from services.build.clean_builder import CleanBuilder
from services.fixtures.manifest_phase2 import (
    Phase2FixtureManifest,
    SameDurationWrongMediaFault,
    StaleCapabilityFault,
)
from services.foundation_io import atomic_write, canonical_model_bytes
from services.job_runner.gate_cp_models import OperationOutcome
from services.job_runner.gate_p2_ir import (
    compile_package,
    production_ir,
    stale_lock_for,
    synthesize_fixture_media,
    wrong_media_presented,
)
from services.job_runner.gate_p2_models import ROUTE_KEYS
from services.job_runner.state_errors import StateStoreError
from services.job_runner.state_store import StateStore
from services.resolve_adapter.errors import PackageCompileError
from services.resolve_adapter.models import ResolvePackage

if TYPE_CHECKING:
    from services.build.builder_models import BuildOutput
    from services.job_runner.gate_p2_live import LiveRig

class StateLease:
    """Exclusive live lease over the Todo-10 SQL lease table."""

    def __init__(self, db_path: Path, resource: str, holder: str, ttl_seconds: int = 3600) -> None:
        self._db_path = db_path
        self._resource = resource
        self._holder = holder
        self._ttl_seconds = ttl_seconds

    def acquire(self) -> None:
        with StateStore.open(self._db_path) as store:
            try:
                store.acquire_lease(
                    resource=self._resource,
                    holder=self._holder,
                    now=int(time.time()),
                    ttl_seconds=self._ttl_seconds,
                )
            except StateStoreError as error:
                if error.code == "lease-held":
                    raise LeaseHeld(str(error)) from error
                raise

    def release(self) -> None:
        try:
            with StateStore.open(self._db_path) as store:
                store.release_lease(
                    resource=self._resource, holder=self._holder, now=int(time.time())
                )
        except StateStoreError:
            return


HUMAN_ROUTE_NAME: Final = "human-route.json"
QC_STATUS_NAME: Final = "qc-status.json"
IR_NAME: Final = "timeline-ir.json"


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, sort_keys=True).encode())


def write_human_route(work: Path, route: str, reason: str) -> str:
    path = work / HUMAN_ROUTE_NAME
    write_json(path, {"human_route": route, "reason": reason, "fixture_only": True})
    return str(path)


def qc_status(work: Path, status: str, reason: str) -> str:
    path = work / QC_STATUS_NAME
    write_json(path, {"qc": status, "reason": reason})
    return str(path)


def route_of(**overrides: str) -> dict[str, str]:
    base: dict[str, str] = dict.fromkeys(ROUTE_KEYS, "")
    base.update(overrides)
    return base


def declared_view(manifest: Phase2FixtureManifest) -> dict[str, str]:
    fault = manifest.fault
    return route_of(
        package_compilation=fault.expected_package_compilation,
        failure_code=getattr(fault, "expected_failure_code", "") or "",
        readback=fault.expected_readback,
        render=fault.expected_render,
        qc=fault.expected_qc,
        retry=fault.expected_retry,
        human_route=fault.expected_human_route,
    )


def fault_probe(work: Path, manifest: Phase2FixtureManifest) -> tuple[OperationOutcome, str]:
    fault = manifest.fault
    try:
        if isinstance(fault, StaleCapabilityFault):
            compile_package(manifest, lock_override=stale_lock_for(manifest))
        elif isinstance(fault, SameDurationWrongMediaFault):
            compile_package(manifest, presented_media=wrong_media_presented(manifest))
        else:  # pragma: no cover - routing table exhaustiveness
            raise TypeError("no compile fault probe for this kind")
    except PackageCompileError as error:
        path = work / "fault-probe.json"
        write_json(path, {"code": error.code, "detail": str(error)})
        return (
            OperationOutcome(
                name="fault-probe", result=f"typed-failure:{error.code}", detail=str(error)
            ),
            error.code,
        )
    return OperationOutcome(name="fault-probe", result="unexpected-success"), ""


def clean_package(work: Path, manifest: Phase2FixtureManifest) -> str:
    package = compile_package(manifest)
    atomic_write(work / "package.json", canonical_model_bytes(package))
    atomic_write(work / IR_NAME, canonical_model_bytes(production_ir(manifest)))
    write_json(
        work / "media.json",
        {
            "declared": manifest.base.declared_media.model_dump(mode="json"),
            "materialized": None,
            "basis": "compile-only fixture: no live build, manifest binding used",
        },
    )
    return str(work / "package.json")


def media_and_package(work: Path, manifest: Phase2FixtureManifest) -> Path:
    binding = synthesize_fixture_media(manifest, work / "media")
    write_json(
        work / "media" / "media.json",
        {
            "declared": manifest.base.declared_media.model_dump(mode="json"),
            "materialized": {"path": binding.path, "sha256": binding.sha256},
            "basis": (
                "fixture synthesis is not byte-reproducible across runs; the "
                "materialized bytes are the live build binding of record while "
                "the declared binding stays the compile-time fault baseline"
            ),
        },
    )
    package = compile_package(manifest, media_binding=(binding,))
    target = work / "live-package.json"
    atomic_write(target, canonical_model_bytes(package))
    atomic_write(work / IR_NAME, canonical_model_bytes(production_ir(manifest)))
    return target


def load_package(path: Path) -> ResolvePackage:
    return ResolvePackage.model_validate_json(path.read_bytes())


def build_live(rig: LiveRig, package: ResolvePackage, work: Path) -> BuildOutput:
    bundle_dir = work / "build"
    lease = StateLease(rig.lease_db, f"phase2-gate:{package.artifact_id}", "phase2-gate")
    return CleanBuilder(rig.connection, rig.wiring(package, bundle_dir)).build(package, lease)


def render_of(output: BuildOutput) -> tuple[Path, str]:
    if output.subtitle is not None:
        return Path(output.subtitle.output_path), output.subtitle.output_sha256
    return Path(output.render.output_path), output.render.output_sha256




__all__ = [
    "StateLease",
    "build_live",
    "clean_package",
    "declared_view",
    "fault_probe",
    "load_package",
    "media_and_package",
    "qc_status",
    "route_of",
    "write_human_route",
    "write_json",
]
