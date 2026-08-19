"""Live fault-scenario drives: kill seam, drift evidence, lying render.

``interrupt_build`` runs one build under the counting-pool kill seam;
``drift_check_partial`` records the typed drift evidence on the partial
orphan staging; ``false_complete_build`` places and reads back truthfully,
then refuses the manifest-declared lying render status through the real
builder render path with a bounded poll. Every drive disposes its owned
``__fvp_test__`` stagings.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from services.build.builder_apply import (
    apply_link_groups,
    ensure_track_layout,
    import_media,
    place_items,
)
from services.build.builder_models import (
    BuildFailure,
    BuildInterrupted,
    RenderTiming,
)
from services.build.builder_recover import sweep_orphan_stagings, verify_declared_media
from services.build.builder_render import render_timeline
from services.build.builder_session import (
    create_staging,
    delete_staging,
    staging_project_name,
)
from services.build.clean_builder import CleanBuilder
from services.build.conformance import item_readback_rows, verify_built_conformance
from services.build.drift import StagingDriftGuard
from services.foundation_io import canonical_model_bytes
from services.job_runner.gate_p2_live import (
    FaultManager,
    FaultProject,
    FaultResolve,
    LiveRig,
    _ProjectSurface,
    owned_build_projects,
)
from services.job_runner.gate_p2_probe import StateLease
from services.resolve_bridge.connection import ResolveApi, ResolveConnection

if TYPE_CHECKING:
    from services.resolve_adapter.models import ResolvePackage
    from services.resolve_bridge.fixed_presentation_models import FixedProjectApi

FAULT_RENDER_DEADLINE_SECONDS: Final = 12.0
FAULT_RENDER_POLL_SECONDS: Final = 1.0


def interrupt_build(
    rig: LiveRig, package: ResolvePackage, interrupt_after: int, evidence_bundle: Path
) -> dict[str, object]:
    """Run one build under the kill seam; return the raw interruption record."""

    fault_manager = FaultManager(
        rig.connection.project_manager(), interrupt_after_placed_items=interrupt_after
    )
    fault_connection = ResolveConnection(
        resolve=cast("ResolveApi", FaultResolve(rig.connection.resolve, fault_manager)),
        binding=rig.connection.binding,
    )
    lease = StateLease(rig.lease_db, f"phase2-gate:{package.artifact_id}", "phase2-gate")
    record: dict[str, object] = {"interrupted": False}
    try:
        CleanBuilder(
            fault_connection, rig.wiring(package, evidence_bundle)
        ).build(package, lease)
    except BuildInterrupted as error:
        record = {"interrupted": True, "detail": str(error)}
    finally:
        lease.release()
    pool = fault_manager.counting_pool
    record["placed_items"] = pool.placed_items if pool else None
    record["interrupt_raised"] = bool(pool and pool.interrupted)
    record["declared_interrupt_after"] = interrupt_after
    record["total_placements"] = len(package.placements)
    record["orphans"] = list(owned_build_projects(rig.connection))
    return record


def drift_check_partial(rig: LiveRig, package: ResolvePackage) -> dict[str, object]:
    """Typed drift evidence that the partial staging is non-conforming."""

    report = StagingDriftGuard().check(
        rig.connection.project_manager(), package, rig.tools().sha256
    )
    if report is None:
        return {"drift_detected": False}
    return {
        "drift_detected": True,
        "kind": report.kind,
        "report": canonical_model_bytes(report).decode(),
    }


def false_complete_build(
    rig: LiveRig,
    package: ResolvePackage,
    lying_status: dict[str, object],
    evidence_bundle: Path,
) -> dict[str, object]:
    """Place + readback truthfully, then refuse the lying render status."""

    tools = rig.tools()
    manager = rig.connection.project_manager()
    sweep_orphan_stagings(manager)
    name = staging_project_name(package.content_hash)
    record: dict[str, object] = {}
    try:
        real_project, timeline = create_staging(manager, name, package)
        fault_project = cast(
            "FixedProjectApi",
            FaultProject(cast("_ProjectSurface", real_project), lying_status=lying_status),
        )
        paths = verify_declared_media(package)
        pool = real_project.GetMediaPool()
        ensure_track_layout(timeline, package)
        media = import_media(pool, paths)
        handles = place_items(pool, package, media)
        apply_link_groups(timeline, package, handles)
        table = verify_built_conformance(timeline, package, tools.sha256)
        record["readback_conformant"] = table.all_passed
        record["readback_items"] = len(item_readback_rows(table))
        record["conformance_table"] = canonical_model_bytes(table).decode()
        try:
            render_timeline(
                fault_project,
                package,
                evidence_bundle / "render",
                tools,
                RenderTiming(
                    deadline_seconds=FAULT_RENDER_DEADLINE_SECONDS,
                    poll_seconds=FAULT_RENDER_POLL_SECONDS,
                ),
            )
        except BuildFailure as error:
            record["render_failure_code"] = error.code
            record["render_failure_detail"] = error.detail
        record["status_polls"] = getattr(fault_project, "status_polls", 0)
    finally:
        delete_staging(manager, name)
    record["swept"] = list(owned_build_projects(rig.connection))
    return record


__all__ = ["drift_check_partial", "false_complete_build", "interrupt_build"]
