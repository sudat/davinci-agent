"""LIVE capability probe: fixed Fairlight track preset / audio-output settings.

Probes whether the documented scripting surface can apply a FIXED audio
track preset (or a project-level stereo-output template) and READ IT BACK.
Every risky live call runs in a CHILD process under a bounded watchdog
(the Todo-58 pattern): a stall is an honest recorded timeout, never a hung
runner, and an apply surface without a matching readback stays
``api_available`` with ``live_verified=false`` — the preset rung stays
unavailable and the ladder resolves to the external mix or manual. New
findings append idempotently to
``capabilities/resolve-21.0.4/audio-preset-findings.json`` (the established
findings-append path; the frozen ``capability-matrix.json`` is untouched).
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
from services.resolve_bridge.audio_preset_probe_models import (
    AudioPresetProbeReport,
    MethodProbe,
    PresetProjectApi,
    PresetStep,
    PresetTimelineApi,
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
from services.spike.gate_models import (
    ApiFindingEntry,
    CapabilityEntry,
    EvidenceRef,
)

MARKER: Final = "audio-preset-probe:"
EXIT_UNAVAILABLE: Final = 4
REPORT_NAME: Final = "audio-preset-probe-report.json"
CHILD_RESULT_NAME: Final = "audio-preset-child-result.json"
FINDINGS_NAME: Final = "audio-preset-findings.json"
DEFAULT_WATCHDOG_SECONDS: Final = 240.0
BUNDLE_NAME: Final = "audio-preset-probe"
PROBE_PRESET_NAME: Final = "Broadcast Loudness Stereo"
CANDIDATE_TIMELINE_METHODS: Final[tuple[str, ...]] = (
    "ApplyTrackPreset",
    "SetTrackPreset",
    "LoadTrackPreset",
    "ApplyAudioTrackPreset",
    "SetAudioTrackPreset",
    "ApplyFairlightPreset",
    "LoadFairlightPreset",
    "NormalizeAudioTrackLevels",
)
CANDIDATE_SETTINGS: Final[tuple[str, ...]] = (
    "timelineAudioOutputChannels",
    "audioOutputChannels",
    "fairlightMainOutputChannels",
    "audioMonitorChannels",
)


def _enumerate_methods(timeline: object) -> tuple[MethodProbe, ...]:
    # hasattr lies on the live binding (unknown names yield non-callables)
    return tuple(
        MethodProbe(name=name, present=callable(getattr(timeline, name, None)))
        for name in CANDIDATE_TIMELINE_METHODS
    )


def _try_apply(timeline: object, methods: tuple[MethodProbe, ...]) -> tuple[bool, str]:
    for row in methods:
        if not row.present:
            continue
        callable_attr = getattr(timeline, row.name, None)
        if not callable(callable_attr):
            continue
        try:
            returned: object
            if row.name == "NormalizeAudioTrackLevels":
                returned = callable_attr("audio", 0)
            else:
                returned = callable_attr("audio", 1, PROBE_PRESET_NAME)
        except Exception as error:  # noqa: BLE001 (record every live failure honestly)
            return False, f"{row.name} raised {type(error).__name__}: {error}"[:280]
        return True, f"{row.name} returned {returned!r}"[:280]
    return False, "no apply-capable method exists on the live timeline"


def _try_readback(timeline: object, applied_detail: str) -> tuple[bool, str]:
    name = applied_detail.split(" ", maxsplit=1)[0]
    for getter in (
        f"Get{name.removeprefix('Apply').removeprefix('Set').removeprefix('Load')}",
        "GetTrackPreset",
        "GetAudioTrackPreset",
    ):
        attribute = getattr(timeline, getter, None)
        if not callable(attribute):
            continue
        try:
            value = attribute("audio", 1)
        except Exception as error:  # noqa: BLE001 (record every live failure honestly)
            return False, f"{getter} raised {type(error).__name__}: {error}"[:280]
        return (
            PROBE_PRESET_NAME in str(value),
            f"{getter} read back {value!r}"[:280],
        )
    return False, "no preset getter exists on the live timeline"


def _probe_settings(project: PresetProjectApi) -> tuple[SettingProbe, ...]:
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


def probe_live(connection: ResolveConnection) -> AudioPresetProbeReport:
    """Run the probe steps; every step records honestly, none can hang."""

    steps: list[PresetStep] = []
    binding = connection.binding.version_string
    manager = connection.project_manager()
    report = AudioPresetProbeReport(
        schema_version="audio-preset-probe-report-v1",
        binding=binding,
        preset_rung="none",
        applied=False,
        readback_verified=False,
        methods=(),
        settings=(),
        steps=(),
    )
    try:
        project_api = create_disposable_project(manager, owned_project_name())
        timeline = cast(
            "PresetTimelineApi", create_owned_timeline(project_api, owned_timeline_name())
        )
        before = timeline.GetTrackCount("audio")
        if not timeline.AddTrack("audio", "stereo"):
            raise ValueError(  # noqa: TRY301 (funnels to the recorded step table)
                'AddTrack("audio", "stereo") failed'
            )
        if timeline.GetTrackCount("audio") <= before:
            raise ValueError(  # noqa: TRY301 (funnels to the recorded step table)
                "stereo audio track creation not observable"
            )
        steps.append(PresetStep(step="owned-timeline", status="verified"))
        methods = _enumerate_methods(timeline)
        steps.append(
            PresetStep(
                step="method-enumeration",
                status="verified",
                detail=f"{sum(1 for m in methods if m.present)} present of "
                f"{len(methods)} candidates",
            )
        )
        applied, apply_detail = _try_apply(timeline, methods)
        steps.append(
            PresetStep(
                step="preset-apply",
                status="verified" if applied else "unsupported",
                detail=apply_detail,
            )
        )
        readback_verified, readback_detail = (False, "not applicable")
        if applied:
            readback_verified, readback_detail = _try_readback(timeline, apply_detail)
            steps.append(
                PresetStep(
                    step="preset-readback",
                    status="verified" if readback_verified else "unsupported",
                    detail=readback_detail,
                )
            )
        settings = _probe_settings(cast("PresetProjectApi", project_api))
        any_roundtrip = any(row.set_roundtrip for row in settings)
        steps.append(
            PresetStep(
                step="setting-roundtrip",
                status="verified" if any_roundtrip else "unsupported",
                detail=f"{sum(1 for r in settings if r.set_roundtrip)} of "
                f"{len(settings)} candidate settings round-tripped",
            )
        )
        rung = "fixed_track_preset" if applied else (
            "project_template" if any_roundtrip else "none"
        )
        return report.model_copy(
            update={
                "preset_rung": rung,
                "applied": applied,
                "readback_verified": readback_verified,
                "methods": methods,
                "settings": settings,
                "steps": tuple(steps),
            }
        )
    except Exception as error:  # noqa: BLE001 (probe records; caller decides)
        steps.append(
            PresetStep(step="probe-infra", status="failed", detail=repr(error)[:300])
        )
        return report.model_copy(update={"steps": tuple(steps)})
    finally:
        try:
            cleanup_owned_projects(manager)
        except Exception as error:  # noqa: BLE001 (cleanup must never mask the result)
            print(f"{MARKER} cleanup warning: {error}", file=sys.stderr)


def derive_preset_findings(
    report: AudioPresetProbeReport, *, report_sha: str
) -> tuple[ApiFindingEntry, ...]:
    """Derive the two Todo-59 findings from the probe evidence, honestly."""

    apply_surface = any(row.present for row in report.methods)
    preset_limits = (
        f"apply surface exists on this host; applied={report.applied}; "
        f"readback_verified={report.readback_verified} — without a matching "
        "getter the applied preset state cannot be verified, so the preset "
        "rung stays unavailable"
        if apply_surface
        else "no fixed-track-preset apply method exists on the live timeline "
        f"(candidates probed: {', '.join(row.name for row in report.methods)}); "
        "fine-grained Fairlight automation stays out of reach of the "
        "documented scripting surface"
    )
    settings_ok = any(row.set_roundtrip for row in report.settings)
    setting_limits = (
        "project audio settings round-trip verified (GetSetting/SetSetting with "
        "identical readback); settings are project-level state, not a fixed "
        "track preset"
        if settings_ok
        else "no candidate project audio setting round-tripped on this host"
    )
    ref = (EvidenceRef(path=f"{BUNDLE_NAME}/{REPORT_NAME}", sha256=report_sha),)
    return (
        ApiFindingEntry(
            finding="fairlight-track-preset-apply",
            api_available=apply_surface,
            live_verified=report.preset_supported,
            evidence_refs=ref,
            limitations=preset_limits,
        ),
        ApiFindingEntry(
            finding="fairlight-audio-setting-roundtrip",
            api_available=True,
            live_verified=settings_ok,
            evidence_refs=ref,
            limitations=setting_limits,
        ),
    )


def preset_capability_row(
    report: AudioPresetProbeReport, ref: tuple[EvidenceRef, ...]
) -> CapabilityEntry:
    """The Todo-59 fairlight_audio_preset row, derived from probe evidence."""

    return CapabilityEntry(
        capability="fairlight_audio_preset",
        api_available=any(row.present for row in report.methods),
        live_verified=report.preset_supported,
        evidence_refs=ref,
        limitations=(
            "fixed track preset apply+readback when live-verified; an apply "
            "surface without readback stays unverified (explicit fallback "
            "only); no fine-grained unverified Fairlight automation"
        ),
    )


def _report_bytes(probe: AudioPresetProbeReport) -> bytes:
    payload: dict[str, object] = json.loads(canonical_model_bytes(probe))
    payload["preset_supported"] = probe.preset_supported
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
        "services.resolve_bridge.audio_preset_probe",
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
        steps = (PresetStep(step="guarded-probe", status="timeout", detail=child.detail),)
        probe = AudioPresetProbeReport(
            schema_version="audio-preset-probe-report-v1",
            binding="unavailable",
            preset_rung="none",
            applied=False,
            readback_verified=False,
            methods=(),
            settings=(),
            steps=steps,
        )
        if outcome == "unavailable":
            print(f"{MARKER} bridge unavailable: {child.detail}", file=sys.stderr)
            return EXIT_UNAVAILABLE
    else:
        probe = AudioPresetProbeReport.model_validate_json(result_path.read_bytes())
    atomic_write(report_path, _report_bytes(probe))
    findings = derive_preset_findings(probe, report_sha=sha256_file(report_path))
    ref = (
        EvidenceRef(
            path=f"{BUNDLE_NAME}/{REPORT_NAME}", sha256=sha256_file(report_path)
        ),
    )
    capability = preset_capability_row(probe, ref)
    verified = tuple(finding for finding in findings if finding.live_verified)
    if verified:
        append_matrix_findings(evidence / FINDINGS_NAME, verified, capability=capability)
        if arguments.findings is not None:
            append_matrix_findings(
                arguments.findings, verified, capability=capability
            )
    supported = probe.preset_supported
    present = sum(1 for row in probe.methods if row.present)
    print(
        f"{MARKER} preset_supported={str(supported).lower()} "
        f"preset_rung={probe.preset_rung} methods={present}/{len(probe.methods)}"
    )
    for step in probe.steps:
        print(f"{MARKER} step={step.step} status={step.status} {step.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
