"""Offline fault scenarios and fakes for Resolve bridge fail-closed QA.

``FaultSpec`` JSON fixtures (wired through the ``QA_FAULT_FIXTURE``
environment variable) drive the same connection/lifecycle code paths against
in-memory fakes so drift, remote-endpoint, missing-API, and non-owned-deletion
failures are demonstrable without a live Resolve.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from services.resolve_bridge.connection import (
    BridgeBindingError,
    BridgeUnavailable,
    NonLocalEndpointError,
    ProjectManagerApi,
    ResolveApi,
    ScriptModule,
    connect_with_module,
    load_script_module,
)
from services.resolve_bridge.lifecycle import NonOwnedResourceError, delete_owned_project
from services.resolve_bridge.models import (
    AssetInventory,
    EditionEvidence,
    FileEvidence,
    GpuInventory,
    HostIdentity,
    OutputPosture,
    ResolveApplication,
    ResolveBridge,
    ResolveDocs,
    ResolveHostReport,
    ScriptingPosture,
    StrictModel,
)
from services.resolve_bridge.readiness import APP, SCRIPTING

EXIT_FAULT = 2
EXIT_REFUSED = 3
_SYNTHETIC_SHA = "1" * 64


class FaultSpecError(Exception):
    """The fault fixture is not a valid fault specification."""


class FaultSpec(StrictModel):
    fault: Literal["wrong_build", "missing_api", "remote_endpoint", "non_owned_delete"]
    live_version: list[int | str] | None = None
    library_path: str | None = None
    module_path: str | None = None
    protected_project: str = "OwnerMain"


def load_fault_spec(path: Path) -> FaultSpec:
    try:
        return FaultSpec.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as error:
        raise FaultSpecError(f"invalid fault fixture {path}: {error}") from error


class FakeProjectManager:
    def __init__(self, names: set[str]) -> None:
        self._names = set(names)
        self._current: str | None = None

    def CreateProject(self, project_name: str) -> None:
        if project_name in self._names:
            return None
        self._names.add(project_name)
        self._current = project_name
        return None

    def LoadProject(self, project_name: str) -> None:
        if project_name not in self._names:
            return None
        self._current = project_name
        return None

    def GetCurrentProject(self) -> None:
        return None

    def CloseProject(self, project: object) -> bool:
        self._current = None
        return True

    def DeleteProject(self, project_name: str) -> bool:
        if project_name == self._current or project_name not in self._names:
            return False
        self._names.discard(project_name)
        return True

    def SaveProject(self) -> bool:
        return True

    def GetProjectListInCurrentFolder(self) -> list[str]:
        return sorted(self._names)


class FakeResolve:
    def __init__(self, version: list[int | str]) -> None:
        self._version = version
        self.manager = FakeProjectManager({"OwnerMain"})

    def GetProductName(self) -> str:
        return "DaVinci Resolve Studio"

    def GetVersion(self) -> list[int | str]:
        return self._version

    def GetVersionString(self) -> str:
        core = ".".join(str(part) for part in self._version[:3])
        return f"{core}.{self._version[3]}"

    def GetProjectManager(self) -> ProjectManagerApi:
        return self.manager


class FakeScriptModule:
    def __init__(self, resolve: FakeResolve | None) -> None:
        self._resolve = resolve

    def scriptapp(self, app_name: str) -> ResolveApi | None:
        return self._resolve


def _synthetic_report(spec: FaultSpec) -> ResolveHostReport:
    library = spec.library_path or str(APP / "Contents/Libraries/Fusion/fusionscript.so")
    module = spec.module_path or str(SCRIPTING / "Modules/DaVinciResolveScript.py")
    return ResolveHostReport(
        host=HostIdentity(macos_version="15.0", macos_build="24A335", architecture="arm64"),
        application=ResolveApplication(
            present=True,
            app_path=str(APP),
            info_plist=FileEvidence(path="/synthetic-info.plist", sha256=_SYNTHETIC_SHA),
            bundle_id="com.blackmagic-design.DaVinciResolve",
            version="21.0.4",
            build="21.0.40005",
            edition=EditionEvidence(
                value="unverified",
                evidence=("synthetic fault scenario",),
                needs_live_verification=True,
            ),
        ),
        docs=ResolveDocs(
            app_bundle_paths=(),
            installed_paths=(FileEvidence(path="/synthetic-readme.txt", sha256=_SYNTHETIC_SHA),),
            location_note="synthetic fault scenario",
        ),
        bridge=ResolveBridge(
            library=FileEvidence(path=library, sha256=_SYNTHETIC_SHA),
            module=FileEvidence(path=module, sha256=_SYNTHETIC_SHA),
        ),
        scripting=ScriptingPosture(
            preference_paths=(),
            remote_access="unknown",
            runtime_policy="loopback-only",
            network_access_performed=False,
            needs_live_verification=True,
            reason="synthetic fault scenario",
        ),
        gpu=GpuInventory(backend="metal", inventory="synthetic"),
        assets=AssetInventory(
            font_paths=(), lut_paths=(), fairlight_paths=(), plugin_paths=(), preset_paths=()
        ),
        output=OutputPosture(cwd="/synthetic", writable=True),
    )


@dataclass(frozen=True, slots=True)
class FaultScenario:
    report: ResolveHostReport
    module: ScriptModule
    resolve: ResolveApi | None = None


def build_scenario(spec: FaultSpec) -> FaultScenario:
    report = _synthetic_report(spec)
    if spec.fault in ("remote_endpoint", "missing_api"):
        return FaultScenario(report=report, module=FakeScriptModule(None))
    version = spec.live_version if spec.live_version is not None else [21, 0, 3, 3, ""]
    resolve = FakeResolve(version)
    return FaultScenario(report=report, module=FakeScriptModule(resolve), resolve=resolve)


def _run_non_owned_fault(spec: FaultSpec, scenario: FaultScenario) -> int:
    resolve = scenario.resolve
    if resolve is None:
        print("ERROR: fault scenario has no resolve object", file=sys.stderr)
        return EXIT_FAULT
    manager = resolve.GetProjectManager()
    try:
        delete_owned_project(manager, spec.protected_project)
    except NonOwnedResourceError as error:
        print(error, file=sys.stderr)
        if spec.protected_project in manager.GetProjectListInCurrentFolder():
            print(f"protected-project-intact: {spec.protected_project}", file=sys.stderr)
            return EXIT_REFUSED
        print("ERROR: protected project vanished during refusal", file=sys.stderr)
        return EXIT_FAULT
    print("ERROR: non-owned project deletion was not refused", file=sys.stderr)
    return EXIT_FAULT


def run_fault_cli(spec_path: Path) -> int:
    spec = load_fault_spec(spec_path)
    scenario = build_scenario(spec)
    if spec.fault == "remote_endpoint":
        try:
            load_script_module(scenario.report)
        except NonLocalEndpointError as error:
            print(error, file=sys.stderr)
            return EXIT_FAULT
        print("ERROR: remote endpoint was not refused", file=sys.stderr)
        return EXIT_FAULT
    if spec.fault in ("wrong_build", "missing_api"):
        try:
            connect_with_module(scenario.report, scenario.module)
        except (BridgeBindingError, BridgeUnavailable) as error:
            print(error, file=sys.stderr)
            return EXIT_FAULT
        print("ERROR: drifted bridge was accepted", file=sys.stderr)
        return EXIT_FAULT
    return _run_non_owned_fault(spec, scenario)
