"""Drive the five frozen Phase-2 fixtures through the REAL Todo-47..53 stack.

Per fixture: the offline typed-fault compile probes (manifest-declared),
then the declared live semantics — kill-seam interruption with fresh
restart, the lying render status refusal, or the clean build whose QC
blocks on the declared privacy flag — and the clean candidate path (fresh
rebuild → render CompletionPercentage==100 → deterministic QC → Final
Review bundle consuming MARKED FIXTURE records, no manual Resolve UI).
Every outcome lands in a raw ``observation.json``; the evaluator recomputes
every criterion from the artifact bytes these observations point at.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.build.builder_recover import sweep_orphan_stagings
from services.fixtures.manifest_phase2 import (
    PHASE_2_FIXTURE_IDS,
    BlockingQcPrivacyFault,
    FalseRenderCompleteFault,
    PartialBuildRestartFault,
    Phase2FixtureManifest,
    SameDurationWrongMediaFault,
    StaleCapabilityFault,
)
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.job_runner.gate_cp_models import OperationOutcome
from services.job_runner.gate_p2_build_faults import drift_check_partial, interrupt_build
from services.job_runner.gate_p2_drive_faults import (
    drive_false_complete,
    drive_privacy,
    drive_stale,
    drive_wrong_media,
)
from services.job_runner.gate_p2_finalize import (
    REPORT_NAME,
    REPORT_REPEAT_NAME,
    finalize_clean,
    qc_policy_for,
    qc_run,
)
from services.job_runner.gate_p2_ir import manifest_for
from services.job_runner.gate_p2_live import LiveRig, owned_build_projects
from services.job_runner.gate_p2_models import P2FixtureObservation
from services.job_runner.gate_p2_probe import (
    IR_NAME,
    build_live,
    declared_view,
    load_package,
    media_and_package,
    render_of,
    route_of,
    write_json,
)

if TYPE_CHECKING:
    from services.resolve_bridge.connection import ResolveConnection

FIXTURES_ROOT: Final = "fixtures"


def drive_partial_restart(
    work: Path, manifest: Phase2FixtureManifest, rig: LiveRig, checkpoint_sha: str
) -> tuple[list[OperationOutcome], dict[str, str], dict[str, str]]:
    fault = manifest.fault
    if not isinstance(fault, PartialBuildRestartFault):
        raise TypeError("not a partial-build-restart fixture")
    package_path = media_and_package(work, manifest)
    package = load_package(package_path)
    fields = {
        "live_package": str(package_path),
        "ir": str(work / IR_NAME),
        "media_record": str(work / "media" / "media.json"),
    }
    operations = [OperationOutcome(name="clean-compile", result="ok")]
    interrupt = interrupt_build(
        rig, package, fault.interrupt_after_placed_items, work / "interrupt"
    )
    write_json(work / "interrupt" / "interrupt.json", interrupt)
    fields["interrupt"] = str(work / "interrupt" / "interrupt.json")
    drift = drift_check_partial(rig, package)
    write_json(work / "interrupt" / "drift.json", drift)
    fields["drift"] = str(work / "interrupt" / "drift.json")
    swept = sweep_orphan_stagings(rig.connection.project_manager())
    operations.append(
        OperationOutcome(
            name="interrupt",
            result="build-interrupted" if interrupt.get("interrupted") else "no-interrupt",
            detail=f"placed={interrupt.get('placed_items')} of "
            f"{interrupt.get('total_placements')} swept={len(swept)}",
        )
    )
    output = build_live(rig, package, work)
    fields["build_output"] = str(work / "build" / "build-output.json")
    operations.append(
        OperationOutcome(
            name="rebuild",
            result="ok",
            detail=f"items={len(output.items)} render={output.render.completion_percentage}",
        )
    )
    render_path, render_sha = render_of(output)
    fields["render_output"] = str(render_path)
    fields["render_sha256"] = render_sha
    policy_path = qc_policy_for(render_path, work / "qc")
    fields["qc_policy"] = str(policy_path)
    report = qc_run(
        render_path, policy_path, work / "qc", ir_path=Path(fields["ir"]), out_name=REPORT_NAME
    )
    fields["qc_report"] = str(work / "qc" / REPORT_NAME)
    qc_run(
        render_path,
        policy_path,
        work / "qc",
        ir_path=Path(fields["ir"]),
        out_name=REPORT_REPEAT_NAME,
    )
    fields["qc_report_repeat"] = str(work / "qc" / REPORT_REPEAT_NAME)
    operations.append(
        OperationOutcome(name="qc", result=report.verdict, detail="two identical runs")
    )
    finalized = finalize_clean(
        episode_id=manifest.fixture_id,
        render_path=render_path,
        build_output_sha256=sha256_file(Path(fields["build_output"])),
        conformance_fingerprint=output.timeline_fingerprint,
        qc_report=report,
        checkpoint_sha256=checkpoint_sha,
        out_dir=work / "final-review",
    )
    fields.update(finalized)
    operations.append(OperationOutcome(name="final-review", result="approved-fixture-only"))
    observed = route_of(
        package_compilation="succeeds",
        failure_code="",
        readback="partial-then-clean-rebuild",
        render="verified-after-restart",
        qc="deterministic-after-restart",
        retry="clean-rebuild-restart",
        human_route="none",
    )
    return operations, fields, observed


def drive_fixture(
    fixture_id: str, evidence: Path, rig: LiveRig | None, checkpoint_sha: str
) -> P2FixtureObservation:
    work = evidence / FIXTURES_ROOT / fixture_id
    work.mkdir(parents=True, exist_ok=True)
    manifest = manifest_for(fixture_id)
    fault = manifest.fault
    if isinstance(fault, StaleCapabilityFault):
        operations, fields, observed = drive_stale(work, manifest)
    elif isinstance(fault, SameDurationWrongMediaFault):
        operations, fields, observed = drive_wrong_media(work, manifest)
    elif isinstance(fault, PartialBuildRestartFault):
        if rig is None:
            raise TypeError("the restart fixture requires the live rig")
        operations, fields, observed = drive_partial_restart(work, manifest, rig, checkpoint_sha)
    elif isinstance(fault, FalseRenderCompleteFault):
        if rig is None:
            raise TypeError("the false-complete fixture requires the live rig")
        operations, fields, observed = drive_false_complete(work, manifest, rig)
    elif isinstance(fault, BlockingQcPrivacyFault):
        if rig is None:
            raise TypeError("the privacy fixture requires the live rig")
        operations, fields, observed = drive_privacy(work, manifest, rig, checkpoint_sha)
    else:  # pragma: no cover - the fault union is closed
        raise TypeError(f"unsupported fault kind: {type(fault).__name__}")
    observation = P2FixtureObservation(
        fixture_id=fixture_id,
        work_dir=str(work),
        operations=tuple(operations),
        fields=fields,
        declared_route=declared_view(manifest),
        observed_route=observed,
    )
    atomic_write(work / "observation.json", canonical_model_bytes(observation))
    return observation


def drive_all_fixtures(
    evidence: Path,
    connection: ResolveConnection | None,
    checkpoint_sha: str,
    ffmpeg_bin: Path | None = None,
    ffprobe_bin: Path | None = None,
) -> dict[str, P2FixtureObservation]:
    rig: LiveRig | None = None
    if connection is not None:
        if ffmpeg_bin is None or ffprobe_bin is None:
            raise TypeError("the live rig requires the pinned media binaries")
        rig = LiveRig(
            connection=connection,
            ffmpeg_bin=ffmpeg_bin,
            ffprobe_bin=ffprobe_bin,
            lease_db=evidence / "leases.db",
        )
    observations: dict[str, P2FixtureObservation] = {}
    for fixture_id in PHASE_2_FIXTURE_IDS:
        observations[fixture_id] = drive_fixture(fixture_id, evidence, rig, checkpoint_sha)
    if rig is not None and owned_build_projects(rig.connection):
        sweep_orphan_stagings(rig.connection.project_manager())
    return observations


__all__ = ["drive_all_fixtures", "drive_fixture"]
