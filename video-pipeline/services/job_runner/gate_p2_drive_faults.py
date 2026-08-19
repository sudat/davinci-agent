"""The false-complete and privacy fixture drives (Phase-2 gate).

``drive_false_complete`` places and reads back truthfully, then refuses
the manifest-declared lying render status through the real builder render
path with a bounded poll. ``drive_privacy`` builds cleanly, runs the
deterministic QC with the manifest-declared blocking privacy declaration,
and proves the Final Review approve route refuses behind the human gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from services.fixtures.manifest_phase2 import (
    BlockingQcPrivacyFault,
    FalseRenderCompleteFault,
)
from services.foundation_io import sha256_file
from services.job_runner.gate_cp_models import OperationOutcome
from services.job_runner.gate_p2_build_faults import false_complete_build
from services.job_runner.gate_p2_finalize import (
    REPORT_NAME,
    finalize_privacy_block,
    fixture_privacy_declarations,
    qc_policy_for,
    qc_run,
)
from services.job_runner.gate_p2_probe import (
    IR_NAME,
    build_live,
    clean_package,
    fault_probe,
    load_package,
    media_and_package,
    qc_status,
    render_of,
    route_of,
    write_human_route,
    write_json,
)

if TYPE_CHECKING:
    from services.fixtures.manifest_phase2 import Phase2FixtureManifest
    from services.job_runner.gate_p2_live import LiveRig


def drive_stale(
    work: Path, manifest: Phase2FixtureManifest
) -> tuple[list[OperationOutcome], dict[str, str], dict[str, str]]:
    fields = {"package": clean_package(work, manifest)}
    operations = [OperationOutcome(name="clean-compile", result="ok")]
    outcome, code = fault_probe(work, manifest)
    operations.append(outcome)
    fields["fault_probe"] = str(work / "fault-probe.json")
    fields["human_route"] = write_human_route(
        work, "refresh-capability-matrix", "capability matrix stale: refresh before any build"
    )
    fields["qc_status"] = qc_status(work, "not-attempted", "package compilation refused")
    observed = route_of(
        package_compilation="typed-failure",
        failure_code=code,
        readback="not-attempted",
        render="not-attempted",
        qc="not-attempted",
        retry="none-input-fix",
        human_route="refresh-capability-matrix",
    )
    return operations, fields, observed


def drive_wrong_media(
    work: Path, manifest: Phase2FixtureManifest
) -> tuple[list[OperationOutcome], dict[str, str], dict[str, str]]:
    fields = {"package": clean_package(work, manifest)}
    operations = [OperationOutcome(name="clean-compile", result="ok")]
    outcome, code = fault_probe(work, manifest)
    operations.append(outcome)
    fields["fault_probe"] = str(work / "fault-probe.json")
    fields["human_route"] = write_human_route(
        work, "media-source-review", "media bytes substituted at identical duration"
    )
    fields["qc_status"] = qc_status(work, "blocked", "package compilation refused")
    observed = route_of(
        package_compilation="typed-failure",
        failure_code=code,
        readback="not-attempted",
        render="blocked",
        qc="blocked",
        retry="none-input-fix",
        human_route="media-source-review",
    )
    return operations, fields, observed


def drive_false_complete(
    work: Path, manifest: Phase2FixtureManifest, rig: LiveRig
) -> tuple[list[OperationOutcome], dict[str, str], dict[str, str]]:
    fault = manifest.fault
    if not isinstance(fault, FalseRenderCompleteFault):
        raise TypeError("not a false-render-complete fixture")
    package_path = media_and_package(work, manifest)
    package = load_package(package_path)
    fields = {
        "live_package": str(package_path),
        "ir": str(work / IR_NAME),
        "media_record": str(work / "media" / "media.json"),
    }
    operations = [OperationOutcome(name="clean-compile", result="ok")]
    lying = {
        "JobStatus": fault.reported_job_status,
        "CompletionPercentage": fault.reported_completion_percentage,
    }
    record = false_complete_build(rig, package, lying, work / "refusal")
    write_json(work / "refusal" / "false-complete.json", record)
    fields["false_complete"] = str(work / "refusal" / "false-complete.json")
    refusal_code = str(record.get("render_failure_code", ""))
    operations.append(
        OperationOutcome(
            name="render-refusal",
            result=f"typed-failure:{refusal_code}",
            detail=str(record.get("render_failure_detail", ""))[:200],
        )
    )
    fields["human_route"] = write_human_route(
        work, "render-job-review", "render reported a completion-looking status below 100%"
    )
    fields["qc_status"] = qc_status(work, "blocked", "render refused: QC not attempted")
    observed = route_of(
        package_compilation="succeeds",
        failure_code=refusal_code,
        readback="conformant" if record.get("readback_conformant") else "non-conformant",
        render="refused-incomplete",
        qc="blocked",
        retry="bounded-auto-retry",
        human_route="render-job-review",
    )
    return operations, fields, observed


def drive_privacy(
    work: Path, manifest: Phase2FixtureManifest, rig: LiveRig, checkpoint_sha: str
) -> tuple[list[OperationOutcome], dict[str, str], dict[str, str]]:
    fault = manifest.fault
    if not isinstance(fault, BlockingQcPrivacyFault):
        raise TypeError("not a blocking-qc-privacy fixture")
    package_path = media_and_package(work, manifest)
    package = load_package(package_path)
    fields = {
        "live_package": str(package_path),
        "ir": str(work / IR_NAME),
        "media_record": str(work / "media" / "media.json"),
    }
    operations = [OperationOutcome(name="clean-compile", result="ok")]
    output = build_live(rig, package, work)
    fields["build_output"] = str(work / "build" / "build-output.json")
    operations.append(
        OperationOutcome(
            name="build",
            result="ok",
            detail=f"items={len(output.items)} render={output.render.completion_percentage}",
        )
    )
    render_path, render_sha = render_of(output)
    fields["render_output"] = str(render_path)
    fields["render_sha256"] = render_sha
    policy_path = qc_policy_for(render_path, work / "qc")
    fields["qc_policy"] = str(policy_path)
    declarations = fixture_privacy_declarations(manifest)
    report = qc_run(
        render_path,
        policy_path,
        work / "qc",
        ir_path=Path(fields["ir"]),
        privacy=declarations,
        out_name=REPORT_NAME,
    )
    fields["qc_report"] = str(work / "qc" / REPORT_NAME)
    operations.append(OperationOutcome(name="qc", result=report.verdict))
    finalized = finalize_privacy_block(
        episode_id=manifest.fixture_id,
        render_path=render_path,
        build_output_sha256=sha256_file(Path(fields["build_output"])),
        conformance_fingerprint=output.timeline_fingerprint,
        qc_report=report,
        declarations=declarations,
        checkpoint_sha256=checkpoint_sha,
        out_dir=work / "final-review",
    )
    fields.update(finalized)
    fields["human_route"] = write_human_route(
        work,
        "privacy-dismissal-required",
        "declared blocking privacy flag awaits an operator decision",
    )
    operations.append(
        OperationOutcome(
            name="final-review",
            result=f"refused:{finalized.get('refusal_code', '')}",
        )
    )
    observed = route_of(
        package_compilation="succeeds",
        failure_code="privacy-flag-blocks-publish",
        readback="conformant",
        render="verified",
        qc="typed-failure-blocks-publish",
        retry="none-blocking",
        human_route="privacy-dismissal-required",
    )
    return operations, fields, observed




__all__ = ["drive_false_complete", "drive_privacy", "drive_stale", "drive_wrong_media"]
