"""LIVE capability probe: Text+ (Fusion title) placement + control readback.

Probes the documented surface only (Timeline.InsertFusionTitleIntoTimeline
with the app's Edit Titles templates, Fusion comp/tool access, and the
published StyledText/Size/Center controls). Every risky live call runs in
a CHILD process under a bounded watchdog — the 0A finding (AppendToTimeline
with an SRT item deadlocks) proved unguarded probes can wedge a runner —
and findings are appended to the published Todo-58 findings file with the
established evidence-ref-bound idempotent append (identical duplicates are
no-ops; conflicting duplicates are loud errors). The frozen phase-2 lock
pins ``capability-matrix.json`` bytes, so new findings live in
``capabilities/resolve-21.0.4/title-probe-findings.json`` under the same
capability-matrix-v1 schema.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from services.foundation_io import (
    atomic_write,
    canonical_model_bytes,
    sha256_file,
)
from services.resolve_bridge.connection import (
    BridgeConnectionError,
    ProjectApi,
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
from services.resolve_bridge.title_probe_models import (
    ChildOutcome,
    ControlProbe,
    FusionCompApi,
    FusionToolApi,
    PlacementReadback,
    ProbeStep,
    TitleItemApi,
    TitleProbeReport,
    TitleTimelineApi,
)
from services.spike.gate_models import (
    ApiFindingEntry,
    CapabilityEntry,
    CapabilityMatrix,
    EvidenceRef,
)

if TYPE_CHECKING:
    from services.resolve_bridge.fixed_presentation_models import FixedProjectApi

MARKER: Final = "title-probe:"
EXIT_UNAVAILABLE: Final = 4
REPORT_NAME: Final = "title-probe-report.json"
CHILD_RESULT_NAME: Final = "title-probe-child-result.json"
FINDINGS_NAME: Final = "title-probe-findings.json"
DEFAULT_WATCHDOG_SECONDS: Final = 240.0
READBACK_EPSILON: Final = 1e-6
PROBE_CONTROL_VALUES: Final[tuple[tuple[str, str, float, tuple[float, float]], ...]] = (
    ("StyledText", "FVP PROBE TITLE", 0.0, (0.0, 0.0)),
    ("Size", "0.083", 0.083, (0.0, 0.0)),
    ("Center", "(0.1, 0.9)", 0.0, (0.1, 0.9)),
)


def run_guarded_child(
    argv: tuple[str, ...], *, timeout_seconds: float, result_path: Path
) -> str:
    """Run a child under a watchdog; a stall is recorded, never hung."""

    try:
        completed = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired:
        atomic_write(
            result_path,
            canonical_model_bytes(ChildOutcome(watchdog="timeout", detail="watchdog expired")),
        )
        return "timeout"
    if completed.returncode == EXIT_UNAVAILABLE:
        atomic_write(
            result_path,
            canonical_model_bytes(
                ChildOutcome(watchdog="unavailable", detail=completed.stderr.strip()[-500:])
            ),
        )
        return "unavailable"
    if completed.returncode != 0 or not result_path.is_file():
        atomic_write(
            result_path,
            canonical_model_bytes(
                ChildOutcome(
                    watchdog="crash",
                    detail=(completed.stderr or completed.stdout).strip()[-500:],
                )
            ),
        )
        return "crash"
    return "completed"


def _frame(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(  # noqa: TRY004 (live readback shape is a value failure)
            f"{what} returned a non-numeric value: {value!r}"
        )
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{what} returned a fractional frame: {value!r}")
        return int(value)
    return value


def _tool(comp: FusionCompApi) -> object | None:
    tool = comp.FindToolByID("TextPlus")
    if tool is not None:
        return tool
    return None


def _probe_controls(tool: FusionToolApi) -> tuple[ControlProbe, ...]:
    rows: list[ControlProbe] = []
    for control, text_value, number_value, point_value in PROBE_CONTROL_VALUES:
        value: object
        if control == "StyledText":
            value = text_value
        elif control == "Size":
            value = number_value
        else:
            value = point_value
        before = tool.GetInput(control)
        returned = tool.SetInput(control, value)
        after = tool.GetInput(control)
        rows.append(
            ControlProbe(
                control=control,
                write_value_text=text_value if control == "StyledText" else (
                    str(number_value) if control == "Size" else str(point_value)
                ),
                read_before=repr(before)[:200],
                write_returned=repr(returned)[:60],
                read_after=repr(after)[:200],
                readback_matches=_matches(control, after, value),
            )
        )
    return tuple(rows)


def _numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _matches(control: str, observed: object, wanted: object) -> bool:
    if control == "StyledText":
        return isinstance(observed, str) and observed == wanted
    if control == "Size":
        number = _numeric(observed)
        target = _numeric(wanted)
        return (
            number is not None and target is not None
            and abs(number - target) <= READBACK_EPSILON
        )
    if isinstance(observed, dict):
        point = cast("tuple[float, float]", wanted)
        first = _numeric(observed.get(1))
        second = _numeric(observed.get(2))
        return (
            first is not None
            and second is not None
            and abs(first - point[0]) <= READBACK_EPSILON
            and abs(second - point[1]) <= READBACK_EPSILON
        )
    return False


def probe_live(connection: ResolveConnection) -> TitleProbeReport:  # noqa: C901 (linear honest probe; raises funnel to the recorded step table)
    """Run the probe steps; every step records honestly, none can hang the parent."""

    steps: list[ProbeStep] = []
    binding = connection.binding.version_string
    manager = connection.project_manager()
    report = TitleProbeReport(
        schema_version="title-probe-report-v1",
        binding=binding,
        place_rung="none",
        placed=False,
        structural=None,
        comp_accessed=False,
        tool_found=False,
        controls=(),
        steps=(),
    )
    try:
        project = cast(
            "FixedProjectApi", create_disposable_project(manager, owned_project_name())
        )
        for key, value in (
            ("timelineFrameRate", "30"),
            ("timelineResolutionWidth", "1920"),
            ("timelineResolutionHeight", "1080"),
        ):
            if not project.SetSetting(key, value):
                raise ValueError(  # noqa: TRY301 (funnels to the recorded step table)
                    f"SetSetting({key}) failed"
                )
        timeline = cast(
            "TitleTimelineApi",
            create_owned_timeline(
                cast("ProjectApi", project), owned_timeline_name()
            ),
        )
        if not timeline.SetStartTimecode("01:00:00:00"):
            raise ValueError(  # noqa: TRY301 (funnels to the recorded step table)
                "SetStartTimecode failed"
            )
        steps.append(ProbeStep(step="owned-project", status="verified"))
        item_api: object | None = None
        try:
            item_api = timeline.InsertFusionTitleIntoTimeline("Text+")
        except Exception as error:  # noqa: BLE001 (record every live failure honestly)
            steps.append(
                ProbeStep(
                    step="insert-fusion-title",
                    status="failed",
                    detail=f"{type(error).__name__}: {error}"[:300],
                )
            )
        if item_api is None:
            if not steps or steps[-1].step != "insert-fusion-title":
                steps.append(
                    ProbeStep(
                        step="insert-fusion-title",
                        status="unsupported",
                        detail="InsertFusionTitleIntoTimeline('Text+') returned None",
                    )
                )
            steps.append(
                ProbeStep(
                    step="media-pool-rung",
                    status="unsupported",
                    detail=(
                        "Text+ templates are not exposed as media-pool items through the "
                        "documented scripting surface; AppendToTimeline cannot place them"
                    ),
                )
            )
            return report.model_copy(update={"steps": tuple(steps)})
        steps.append(ProbeStep(step="insert-fusion-title", status="verified"))
        item = cast("TitleItemApi", item_api)
        placement = _readback(item)
        steps.append(ProbeStep(step="structural-readback", status="verified"))
        comp_count = item.GetFusionCompCount()
        comp_api = item.GetFusionCompByIndex(1) if comp_count >= 1 else None
        if comp_api is None:
            steps.append(
                ProbeStep(
                    step="fusion-comp",
                    status="unsupported",
                    detail=f"GetFusionCompCount={comp_count}, no composition at index 1",
                )
            )
            return report.model_copy(
                update={
                    "place_rung": "insert_fusion_title",
                    "placed": True,
                    "structural": placement,
                    "steps": tuple(steps),
                }
            )
        comp = cast("FusionCompApi", comp_api)
        tool_api = _tool(comp)
        if tool_api is None:
            steps.append(
                ProbeStep(
                    step="find-tool",
                    status="unsupported",
                    detail="FindToolByID('TextPlus') found no tool",
                )
            )
            return report.model_copy(
                update={
                    "place_rung": "insert_fusion_title",
                    "placed": True,
                    "structural": placement,
                    "comp_accessed": True,
                    "steps": tuple(steps),
                }
            )
        tool = cast("FusionToolApi", tool_api)
        controls = _probe_controls(tool)
        steps.append(ProbeStep(step="control-readback", status="verified"))
        return report.model_copy(
            update={
                "place_rung": "insert_fusion_title",
                "placed": True,
                "structural": placement,
                "comp_accessed": True,
                "tool_found": True,
                "controls": controls,
                "steps": tuple(steps),
            }
        )
    except Exception as error:  # noqa: BLE001 (probe records; caller decides)
        steps.append(
            ProbeStep(step="probe-infra", status="failed", detail=repr(error)[:300])
        )
        return report.model_copy(update={"steps": tuple(steps)})
    finally:
        try:
            cleanup_owned_projects(manager)
        except Exception as error:  # noqa: BLE001 (cleanup must never mask the result)
            print(f"{MARKER} cleanup warning: {error}", file=sys.stderr)


def _report_bytes(probe: TitleProbeReport) -> bytes:
    payload: dict[str, object] = json.loads(canonical_model_bytes(probe))
    payload["fusion_supported"] = probe.fusion_supported
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _readback(item: TitleItemApi) -> PlacementReadback:
    placement_raw = item.GetTrackTypeAndIndex()
    track_type = str(placement_raw[0]) if placement_raw else "unknown"
    track_index = int(cast("int", placement_raw[1])) if len(placement_raw) > 1 else -1
    return PlacementReadback(
        record_start=_frame(item.GetStart(False), "GetStart"),
        record_end=_frame(item.GetEnd(False), "GetEnd"),
        duration=_frame(item.GetDuration(False), "GetDuration"),
        track_type=track_type,
        track_index=track_index,
    )


def derive_matrix_findings(
    report: TitleProbeReport, *, report_sha: str
) -> tuple[ApiFindingEntry, ...]:
    """Derive the two Todo-58 findings from the probe evidence, honestly."""

    structural_ok = report.placed and report.structural is not None
    placement_limits = (
        "template placement not live-verified on this host"
        if report.structural is None
        else f"InsertFusionTitleIntoTimeline('Text+') places the Edit-page Fusion "
        f"title template; observed record span {report.structural.record_start}.."
        f"{report.structural.record_end} ({report.structural.duration} frames) on "
        f"{report.structural.track_type}/{report.structural.track_index}; the inserted "
        "duration is the template default and duration control is not a published "
        "control; Text+ is not reachable as a media-pool item (AppendToTimeline rung "
        "unsupported by documentation)"
    )
    controls_ok = report.comp_accessed and report.tool_found and report.controls and all(
        row.readback_matches for row in report.controls
    )
    control_limits = (
        "published controls StyledText/Size/Center verified set+readback on the placed "
        "Text+ tool via GetFusionCompByIndex(1)+FindToolByID; SetInput returns None "
        "(verified through readback, not return values); Center is a normalized "
        "{1:x,2:y} point"
        if controls_ok
        else "published-control readback not fully live-verified on this host"
    )
    ref = (EvidenceRef(path=f"title-probe/{REPORT_NAME}", sha256=report_sha),)
    return (
        ApiFindingEntry(
            finding="fusion-title-template-placement",
            api_available=True,
            live_verified=structural_ok,
            evidence_refs=ref,
            limitations=placement_limits,
        ),
        ApiFindingEntry(
            finding="fusion-title-control-readback",
            api_available=True,
            live_verified=bool(controls_ok),
            evidence_refs=ref,
            limitations=control_limits,
        ),
    )


def title_capability_row(
    report: TitleProbeReport, ref: tuple[EvidenceRef, ...]
) -> CapabilityEntry:
    """The Todo-58 fusion_title capability row, derived from probe evidence."""

    return CapabilityEntry(
        capability="fusion_title",
        api_available=True,
        live_verified=report.fusion_supported,
        evidence_refs=ref,
        limitations=(
            "Text+ template placement via Timeline.InsertFusionTitleIntoTimeline with "
            "published controls StyledText/Size/Center (set + readback verified); "
            "duration control is not a published control (template default); no "
            "arbitrary Fusion graph generation"
        ),
    )


def append_matrix_findings(
    target: Path,
    findings: tuple[ApiFindingEntry, ...],
    *,
    capability: CapabilityEntry | None = None,
) -> list[Path]:
    """Idempotent evidence-ref-bound append; conflicting duplicates fail loudly."""

    if target.is_file():
        existing = CapabilityMatrix.model_validate_json(target.read_bytes())
        known = {finding.finding: finding for finding in existing.findings}
        fresh = []
        for finding in findings:
            prior = known.get(finding.finding)
            if prior is None:
                fresh.append(finding)
                continue
            if (
                prior.api_available != finding.api_available
                or prior.live_verified != finding.live_verified
            ):
                raise ValueError(
                    f"appended finding {finding.finding} conflicts with the published "
                    f"verdict (live_verified {prior.live_verified} vs "
                    f"{finding.live_verified})"
                )
        merged_rows: tuple[ApiFindingEntry, ...] = (*existing.findings, *fresh)
        merged_capabilities = existing.capabilities
        if capability is not None and all(
            row.capability != capability.capability for row in existing.capabilities
        ):
            merged_capabilities = (*existing.capabilities, capability)
        if not fresh and merged_capabilities == existing.capabilities:
            return [target]
        merged = existing.model_copy(
            update={"findings": merged_rows, "capabilities": merged_capabilities}
        )
        atomic_write(target, canonical_model_bytes(merged))
        return [target]
    capabilities = (capability,) if capability is not None else ()
    if capability is None:
        capabilities = (
            CapabilityEntry(
                capability="fusion-title-findings",
                api_available=True,
                live_verified=bool(findings) and all(f.live_verified for f in findings),
                evidence_refs=findings[0].evidence_refs if findings else (),
                limitations="findings-only file",
            ),
        )
    matrix = CapabilityMatrix(
        schema_version="capability-matrix-v1",
        resolve_version="21.0.4",
        resolve_build="21.0.40005",
        capabilities=capabilities,
        findings=findings,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, canonical_model_bytes(matrix))
    return [target]


def _child_main(report_path: Path, result_path: Path) -> int:
    try:
        report = load_host_report(report_path)
        connection = connect(report)
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
    bundle = evidence / "title-probe"
    bundle.mkdir(parents=True, exist_ok=True)
    result_path = bundle / CHILD_RESULT_NAME
    report_path = bundle / REPORT_NAME
    argv: tuple[str, ...] = (
        sys.executable,
        "-m",
        "services.resolve_bridge.title_probe",
        "--child",
        "--report",
        str(arguments.report),
        "--result-json",
        str(result_path),
    )
    outcome = run_guarded_child(argv, timeout_seconds=arguments.timeout, result_path=result_path)
    if outcome != "completed":
        child = ChildOutcome.model_validate_json(result_path.read_bytes())
        steps = (ProbeStep(step="guarded-probe", status="timeout", detail=child.detail),)
        probe = TitleProbeReport(
            schema_version="title-probe-report-v1",
            binding="unavailable",
            place_rung="none",
            placed=False,
            structural=None,
            comp_accessed=False,
            tool_found=False,
            controls=(),
            steps=steps,
        )
        if outcome == "unavailable":
            print(f"{MARKER} bridge unavailable: {child.detail}", file=sys.stderr)
            return EXIT_UNAVAILABLE
    else:
        probe = TitleProbeReport.model_validate_json(result_path.read_bytes())
    atomic_write(report_path, _report_bytes(probe))
    findings = derive_matrix_findings(probe, report_sha=sha256_file(report_path))
    ref = (EvidenceRef(path=f"title-probe/{REPORT_NAME}", sha256=sha256_file(report_path)),)
    capability = title_capability_row(probe, ref)
    verified = tuple(finding for finding in findings if finding.live_verified)
    if verified:
        append_matrix_findings(evidence / FINDINGS_NAME, verified, capability=capability)
        if arguments.findings is not None:
            append_matrix_findings(
                arguments.findings, verified, capability=capability
            )
    supported = probe.fusion_supported
    matched = sum(1 for row in probe.controls if row.readback_matches)
    print(
        f"{MARKER} fusion_supported={str(supported).lower()} "
        f"place_rung={probe.place_rung} controls={matched}/{len(probe.controls)}"
    )
    for step in probe.steps:
        print(f"{MARKER} step={step.step} status={step.status} {step.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
