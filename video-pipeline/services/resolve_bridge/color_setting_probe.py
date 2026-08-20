"""LIVE capability probe: project-level color settings (Todo 60).

Probes whether the documented scripting surface can READ and ROUND-TRIP
the project color-science settings (SetSetting/GetSetting on candidate
color keys). Every risky live call runs in a CHILD process under a bounded
watchdog (the Todo-58 pattern): a stall is an honest recorded timeout,
never a hung runner. A key whose value is not readable stays honestly
unverified, and the setting rung is only available when a candidate
round-trips. New findings append idempotently to
``capabilities/resolve-21.0.4/color-setting-findings.json`` (the
established findings-append path; frozen artifacts are untouched).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final, cast

from services.foundation_io import (
    atomic_write,
    canonical_model_bytes,
    sha256_file,
)
from services.presentation.color_profile import COLOR_CAPABILITY, COLOR_FINDING
from services.resolve_bridge.color_setting_probe_models import (
    ColorProbeStep,
    ColorProjectApi,
    ColorSettingProbeReport,
    SettingProbe,
)
from services.resolve_bridge.connection import (
    BridgeConnectionError,
    ResolveConnection,
    connect,
)
from services.resolve_bridge.lifecycle import (
    cleanup_owned_projects,
    create_disposable_project,
    create_owned_timeline,
    owned_project_name,
    owned_timeline_name,
)
from services.resolve_bridge.readiness import load_host_report
from services.resolve_bridge.title_probe import (
    append_matrix_findings,
    run_guarded_child,
)
from services.resolve_bridge.title_probe_models import ChildOutcome
from services.spike.gate_models import ApiFindingEntry, CapabilityEntry, EvidenceRef

MARKER: Final = "color-setting-probe:"
EXIT_UNAVAILABLE: Final = 4
REPORT_NAME: Final = "color-setting-probe-report.json"
CHILD_RESULT_NAME: Final = "color-setting-child-result.json"
FINDINGS_NAME: Final = "color-setting-findings.json"
DEFAULT_WATCHDOG_SECONDS: Final = 240.0
BUNDLE_NAME: Final = "color-setting-probe"
CANDIDATE_SETTINGS: Final[tuple[str, ...]] = (
    "colorScienceMode",
    "hdrColorSpace",
    "timelineWorkingSpace",
    "workingColorSpace",
    "outputColorSpace",
    "colorManagementMode",
    "inputDRT",
    "outputDRT",
)


def _probe_settings(project: ColorProjectApi) -> tuple[SettingProbe, ...]:
    """Set each candidate back to its CURRENT value; readback must match."""

    rows: list[SettingProbe] = []
    for key in CANDIDATE_SETTINGS:
        value = project.GetSetting(key)
        if not value:
            rows.append(SettingProbe(setting=key, value="", set_roundtrip=False))
            continue
        roundtrip = False
        if project.SetSetting(key, value):
            roundtrip = project.GetSetting(key) == value
        rows.append(SettingProbe(setting=key, value=value, set_roundtrip=roundtrip))
    return tuple(rows)


def probe_live(connection: ResolveConnection) -> ColorSettingProbeReport:
    """Run the probe steps; every step records honestly, none can hang."""

    steps: list[ColorProbeStep] = []
    binding = connection.binding.version_string
    report = ColorSettingProbeReport(
        schema_version="color-setting-probe-report-v1",
        binding=binding,
        settings=(),
        steps=(),
    )
    try:
        project_api = create_disposable_project(
            connection.project_manager(), owned_project_name()
        )
        create_owned_timeline(project_api, owned_timeline_name())
        steps.append(ColorProbeStep(step="owned-project", status="verified"))
        settings = _probe_settings(cast("ColorProjectApi", project_api))
        readable = sum(1 for row in settings if row.value)
        roundtrips = sum(1 for row in settings if row.set_roundtrip)
        steps.append(
            ColorProbeStep(
                step="setting-roundtrip",
                status="verified" if roundtrips else "unsupported",
                detail=f"{readable} of {len(settings)} candidates readable; "
                f"{roundtrips} round-tripped",
            )
        )
        return report.model_copy(
            update={"settings": settings, "steps": tuple(steps)}
        )
    except Exception as error:  # noqa: BLE001 (probe records; caller decides)
        steps.append(
            ColorProbeStep(step="probe-infra", status="failed", detail=repr(error)[:300])
        )
        return report.model_copy(update={"steps": tuple(steps)})
    finally:
        try:
            cleanup_owned_projects(connection.project_manager())
        except Exception as error:  # noqa: BLE001 (cleanup must never mask the result)
            print(f"{MARKER} cleanup warning: {error}", file=sys.stderr)


def derive_setting_findings(
    report: ColorSettingProbeReport, *, report_sha: str
) -> tuple[ApiFindingEntry, ...]:
    """Derive the Todo-60 finding from the probe evidence, honestly."""

    readable = sum(1 for row in report.settings if row.value)
    limits = (
        "project color settings round-trip verified (GetSetting/SetSetting with "
        "identical readback); settings are project-level state, applied only "
        "in owned projects"
        if report.any_roundtrip
        else f"no candidate project color setting round-tripped on this host "
        f"({readable} of {len(report.settings)} readable); project color "
        "settings stay unapplied and validation runs externally"
    )
    return (
        ApiFindingEntry(
            finding=COLOR_FINDING,
            api_available=True,
            live_verified=report.any_roundtrip,
            evidence_refs=(
                EvidenceRef(path=f"{BUNDLE_NAME}/{REPORT_NAME}", sha256=report_sha),
            ),
            limitations=limits,
        ),
    )


def setting_capability_row(
    report: ColorSettingProbeReport, ref: tuple[EvidenceRef, ...]
) -> CapabilityEntry:
    """The Todo-60 project_color_settings row, derived from probe evidence."""

    return CapabilityEntry(
        capability=COLOR_CAPABILITY,
        api_available=True,
        live_verified=report.any_roundtrip,
        evidence_refs=ref,
        limitations=(
            "project-level color settings applied via SetSetting only when the "
            "round-trip is live-verified; otherwise the honest rung is external "
            "render-side validation; no per-shot grading anywhere"
        ),
    )


def _report_bytes(probe: ColorSettingProbeReport) -> bytes:
    payload: dict[str, object] = json.loads(canonical_model_bytes(probe))
    payload["any_roundtrip"] = probe.any_roundtrip
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _child_main(report_path: Path, result_path: Path) -> int:
    try:
        connection = connect(load_host_report(report_path))
    except (BridgeConnectionError, OSError) as error:
        print(f"{MARKER} bridge unavailable: {error}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    probe = probe_live(connection)
    atomic_write(result_path, canonical_model_bytes(probe))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--findings", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_WATCHDOG_SECONDS)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--result-json", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.child:
        if arguments.result_json is None:
            print("--result-json is required with --child", file=sys.stderr)
            return 2
        return _child_main(arguments.report, arguments.result_json)
    if arguments.evidence is None:
        print("--evidence is required outside child mode", file=sys.stderr)
        return 2
    evidence: Path = arguments.evidence
    bundle = evidence / BUNDLE_NAME
    bundle.mkdir(parents=True, exist_ok=True)
    result_path = bundle / CHILD_RESULT_NAME
    report_path = bundle / REPORT_NAME
    argv: tuple[str, ...] = (
        sys.executable,
        "-m",
        "services.resolve_bridge.color_setting_probe",
        "--child",
        "--report",
        str(arguments.report),
        "--result-json",
        str(result_path),
    )
    outcome = run_guarded_child(
        argv, timeout_seconds=arguments.timeout, result_path=result_path
    )
    if outcome != "completed":
        child = ChildOutcome.model_validate_json(result_path.read_bytes())
        steps = (
            ColorProbeStep(step="guarded-probe", status="timeout", detail=child.detail),
        )
        probe = ColorSettingProbeReport(
            schema_version="color-setting-probe-report-v1",
            binding="unavailable",
            settings=(),
            steps=steps,
        )
        if outcome == "unavailable":
            print(f"{MARKER} bridge unavailable: {child.detail}", file=sys.stderr)
            return EXIT_UNAVAILABLE
    else:
        probe = ColorSettingProbeReport.model_validate_json(result_path.read_bytes())
    atomic_write(report_path, _report_bytes(probe))
    findings = derive_setting_findings(probe, report_sha=sha256_file(report_path))
    ref = (
        EvidenceRef(
            path=f"{BUNDLE_NAME}/{REPORT_NAME}", sha256=sha256_file(report_path)
        ),
    )
    capability = setting_capability_row(probe, ref)
    verified = tuple(finding for finding in findings if finding.live_verified)
    if verified:
        append_matrix_findings(evidence / FINDINGS_NAME, verified, capability=capability)
        if arguments.findings is not None:
            append_matrix_findings(arguments.findings, verified, capability=capability)
    readable = sum(1 for row in probe.settings if row.value)
    roundtrips = sum(1 for row in probe.settings if row.set_roundtrip)
    print(
        f"{MARKER} any_roundtrip={str(probe.any_roundtrip).lower()} "
        f"readable={readable}/{len(probe.settings)} roundtrips={roundtrips}"
    )
    for step in probe.steps:
        print(f"{MARKER} step={step.step} status={step.status} {step.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
