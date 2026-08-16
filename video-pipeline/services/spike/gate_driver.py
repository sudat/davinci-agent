"""Live driver for the Phase-0A exit-gate evidence matrix.

Executes the six bound spike runs (three, bounded Resolve restart, three
more), the failed-partial-build injection with a fresh-project rebuild, and
the capability probes — all against the frozen manifest and within a total
budget. Writes raw evidence only; pass/fail is recomputed separately by
:mod:`services.spike.gate_evaluate`.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from services.contracts.primitives import RationalFrameRate
from services.fixtures.manifest import Phase0AFixtureManifest
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.resolve_bridge.base_cut_plan import (
    expected_from_manifest,
    fixture_media_map,
    ir_from_manifest,
    request_from_ir,
)
from services.resolve_bridge.build_report import run_spike
from services.resolve_bridge.build_report_fingerprint import (
    requested_placement,
    timeline_fingerprint,
)
from services.resolve_bridge.build_report_models import REPORT_NAME
from services.resolve_bridge.build_report_verify import verify_report
from services.resolve_bridge.connection import BridgeConnectionError, ResolveConnection, connect
from services.resolve_bridge.evidence_recorder import CliRunRecord, invoked_argv, record_cli_run
from services.resolve_bridge.fixed_presentation_models import FRAME_ORIGIN, SUBTITLE_SRT
from services.resolve_bridge.fixed_presentation_tools import MediaTools
from services.resolve_bridge.lifecycle import cleanup_owned_projects
from services.resolve_bridge.readiness import load_host_report
from services.spike.gate_models import (
    GATE_MODULE,
    PARTIAL_ITEMS,
    RECOVERY_DIR,
    RECOVERY_NAME,
    RESTART_DIR,
    RESTART_NAME,
    RUN_COUNT,
    PartialBuildRecord,
    RecoveryRecord,
    run_dir,
)
from services.spike.gate_probes import run_capability_probes
from services.spike.recovery import inject_partial, recording_connection, reread_partial
from services.spike.restart import restart_and_reconnect

if TYPE_CHECKING:
    from services.contracts.build_report import BuildReport0A


class GateDriverError(Exception):
    """The live evidence matrix could not be completed within its bounds."""


class GateUnavailableError(Exception):
    """The live Resolve bridge could not be reached."""


@dataclass(frozen=True, slots=True)
class DriveResult:
    expected_fingerprint: str
    run_fingerprints: tuple[str, ...]
    rebuild_project_name: str
    partial_project_name: str


def _budget_check(deadline: float, step: str) -> None:
    if time.monotonic() > deadline:
        raise GateDriverError(f"gate budget exhausted before {step}")


def _record_run_evidence(out_dir: Path, label: str, report: BuildReport0A, *, passed: bool) -> None:
    record_cli_run(
        out_dir,
        CliRunRecord(
            argv=[*invoked_argv(GATE_MODULE), f"({label})"],
            exit_code=0 if passed else 1,
            expected_exit=0,
            stdout=f"{label}: fingerprint={report.timeline_fingerprint}\n",
            stderr="",
            observe="fingerprint=",
        ),
    )


def execute_run(
    connection: ResolveConnection,
    manifest: Phase0AFixtureManifest,
    manifest_path: Path,
    fixture_dir: Path,
    host_report: Path,
    ffmpeg: Path,
    ffprobe: Path,
    out_dir: Path,
    label: str,
) -> BuildReport0A:
    tools = MediaTools(ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe)
    media = fixture_media_map(fixture_dir)
    request = request_from_ir(ir_from_manifest(manifest), media)
    expected = expected_from_manifest(manifest, media)
    report = run_spike(
        connection,
        request,
        expected,
        manifest,
        media,
        tools,
        out_dir / "render",
        fixture_dir / SUBTITLE_SRT,
        host_report_path=host_report,
        manifest_path=manifest_path,
        ffmpeg_bin=ffmpeg,
        ffprobe_bin=ffprobe,
    )
    outcome = verify_report(
        report,
        manifest,
        fixture_dir,
        tools,
        manifest_path=manifest_path,
        host_report_path=host_report,
        ffmpeg_bin=ffmpeg,
        ffprobe_bin=ffprobe,
    )
    atomic_write(out_dir / REPORT_NAME, canonical_model_bytes(report))
    _record_run_evidence(out_dir, label, report, passed=outcome.passed)
    if not outcome.passed:
        raise GateDriverError(
            f"{label} failed verification: "
            + "; ".join(f"{row.code}: {row.detail}" for row in outcome.mismatches)
        )
    return report


def drive(
    evidence: Path,
    host_report_path: Path,
    manifest_path: Path,
    fixture_dir: Path,
    ffmpeg: Path,
    ffprobe: Path,
    budget_seconds: float,
) -> DriveResult:
    manifest = Phase0AFixtureManifest.model_validate_json(manifest_path.read_bytes())
    media = fixture_media_map(fixture_dir)
    request = request_from_ir(ir_from_manifest(manifest), media)
    expected = expected_from_manifest(manifest, media)
    rate = RationalFrameRate(
        num=manifest.recipe.source.frame_rate.num, den=manifest.recipe.source.frame_rate.den
    )
    expected_fp = timeline_fingerprint(
        tuple(requested_placement(want, rate, FRAME_ORIGIN) for want in expected.items)
    )
    deadline = time.monotonic() + budget_seconds
    host = load_host_report(host_report_path)
    try:
        connection = connect(host)
    except BridgeConnectionError as error:
        raise GateUnavailableError(str(error)) from error

    fingerprints: list[str] = []
    rebuild_project = ""
    partial_name = ""
    try:
        for index in range(1, RUN_COUNT + 1):
            if index == RUN_COUNT // 2 + 1:
                _budget_check(deadline, "restart")
                record, connection = restart_and_reconnect(connection, host)
                atomic_write(
                    evidence / RESTART_DIR / RESTART_NAME, canonical_model_bytes(record)
                )
            _budget_check(deadline, f"run-{index}")
            phase = "before-restart" if index <= RUN_COUNT // 2 else "after-restart"
            report = execute_run(
                connection,
                manifest,
                manifest_path,
                fixture_dir,
                host_report_path,
                ffmpeg,
                ffprobe,
                run_dir(evidence, index),
                f"run-{index}({phase})",
            )
            fingerprint = timeline_fingerprint(tuple(row.observed for row in report.items))
            if fingerprint != expected_fp:
                raise GateDriverError(
                    f"run-{index} fingerprint {fingerprint} != expected {expected_fp}"
                )
            fingerprints.append(fingerprint)

        _budget_check(deadline, "recovery")
        partial_name, timeline_name, snapshot, partial_fp = inject_partial(
            connection, manifest, request, rate, PARTIAL_ITEMS
        )
        if partial_fp == expected_fp:
            raise GateDriverError("partial fingerprint unexpectedly equals the full fingerprint")
        reread_items, reread_fp = reread_partial(connection, partial_name, timeline_name, rate)
        rebuild_dir = evidence / RECOVERY_DIR / "rebuild"
        proxy, tape = recording_connection(connection)
        rebuild = execute_run(
            proxy,
            manifest,
            manifest_path,
            fixture_dir,
            host_report_path,
            ffmpeg,
            ffprobe,
            rebuild_dir,
            "recovery-rebuild",
        )
        rebuild_fp = timeline_fingerprint(tuple(row.observed for row in rebuild.items))
        if rebuild_fp != expected_fp:
            raise GateDriverError(f"rebuild fingerprint {rebuild_fp} != expected {expected_fp}")
        if len(tape.created) != 1:
            raise GateDriverError(f"rebuild created {len(tape.created)} projects")
        rebuild_project = tape.created[0]
        if rebuild_project == partial_name or partial_name in tape.loaded:
            raise GateDriverError("rebuild touched the abandoned partial project")
        recovery = RecoveryRecord(
            partial=PartialBuildRecord(
                project_name=partial_name,
                timeline_name=timeline_name,
                items_placed=len(snapshot.items),
                partial_fingerprint=partial_fp,
            ),
            rebuild_report_rel=f"{RECOVERY_DIR}/rebuild/{REPORT_NAME}",
            rebuild_report_sha256=sha256_file(rebuild_dir / REPORT_NAME),
            rebuild_project_name=rebuild_project,
            rebuild_fingerprint=rebuild_fp,
            rebuild_loaded_partial=partial_name in tape.loaded,
            partial_reread_items=reread_items,
            partial_reread_fingerprint=reread_fp,
            partial_deleted=partial_name
            not in proxy.project_manager().GetProjectListInCurrentFolder(),
        )
        atomic_write(evidence / RECOVERY_DIR / RECOVERY_NAME, canonical_model_bytes(recovery))

        _budget_check(deadline, "probes")
        run_capability_probes(connection, manifest, request, rate, evidence)
    finally:
        try:
            cleanup_owned_projects(connection.project_manager())
        except Exception as cleanup_error:  # noqa: BLE001 -- never mask the primary failure
            print(f"warning: post-drive owned cleanup failed: {cleanup_error}", file=sys.stderr)

    return DriveResult(
        expected_fingerprint=expected_fp,
        run_fingerprints=tuple(fingerprints),
        rebuild_project_name=rebuild_project,
        partial_project_name=partial_name,
    )
