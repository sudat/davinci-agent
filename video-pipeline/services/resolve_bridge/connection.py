"""Loopback-only loader and connector for the host's official Resolve scripting bridge.

Loads exactly the hash-verified ``DaVinciResolveScript.py`` recorded in the
readiness report, refuses any non-local endpoint, and fails closed when the live
Resolve version/build drifts from the report binding.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

from services.foundation_io import sha256_file
from services.resolve_bridge.readiness import APP, SCRIPTING

if TYPE_CHECKING:
    from services.resolve_bridge.models import ResolveHostReport

LOCAL_LIBRARY_ROOT: Final = str(APP / "Contents/Libraries/Fusion")
MIN_VERSION_FIELDS: Final = 4
ALLOWED_PRODUCTS: Final = frozenset({"DaVinci Resolve", "DaVinci Resolve Studio"})


class BridgeConnectionError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class NonLocalEndpointError(BridgeConnectionError):
    """A configured bridge endpoint is not a local installed path."""


class BridgeUnavailable(BridgeConnectionError):
    """The local Resolve application is not accepting scripting connections."""


class BridgeBindingError(BridgeConnectionError):
    """The live bridge does not match the readiness report binding."""


class TimelineApi(Protocol):
    def GetName(self) -> str: ...


class MediaPoolApi(Protocol):
    def CreateEmptyTimeline(self, name: str) -> TimelineApi | None: ...


class ProjectApi(Protocol):
    def GetName(self) -> str: ...
    def GetMediaPool(self) -> MediaPoolApi | None: ...
    def GetTimelineCount(self) -> int: ...
    def GetTimelineByIndex(self, index: int) -> TimelineApi | None: ...
    def SetCurrentTimeline(self, timeline: TimelineApi) -> bool: ...


class ProjectManagerApi(Protocol):
    def CreateProject(self, project_name: str) -> ProjectApi | None: ...
    def LoadProject(self, project_name: str) -> ProjectApi | None: ...
    def GetCurrentProject(self) -> ProjectApi | None: ...
    def CloseProject(self, project: ProjectApi) -> bool: ...
    def DeleteProject(self, project_name: str) -> bool: ...
    def SaveProject(self) -> bool: ...
    def GetProjectListInCurrentFolder(self) -> list[str]: ...


class ResolveApi(Protocol):
    def GetProductName(self) -> str: ...
    def GetVersion(self) -> list[int | str]: ...
    def GetVersionString(self) -> str: ...
    def GetProjectManager(self) -> ProjectManagerApi: ...


class ScriptModule(Protocol):
    def scriptapp(self, app_name: str) -> ResolveApi | None: ...


@dataclass(frozen=True, slots=True)
class VersionBinding:
    product_name: str
    version_core: str
    build_number: int
    version_string: str


@dataclass(frozen=True, slots=True)
class ResolveConnection:
    resolve: ResolveApi
    binding: VersionBinding

    def project_manager(self) -> ProjectManagerApi:
        return self.resolve.GetProjectManager()


def _require_local_paths(report: ResolveHostReport) -> None:
    for label, path in (
        ("bridge library", report.bridge.library.path),
        ("script module", report.bridge.module.path),
    ):
        if "://" in path or not path.startswith("/"):
            raise NonLocalEndpointError(f"refusing non-local bridge endpoint ({label}): {path}")
    if not report.bridge.library.path.startswith(LOCAL_LIBRARY_ROOT):
        raise NonLocalEndpointError(
            f"bridge library outside the local app bundle: {report.bridge.library.path}"
        )
    if not report.bridge.module.path.startswith(str(SCRIPTING)):
        raise NonLocalEndpointError(
            f"script module outside the local scripting dir: {report.bridge.module.path}"
        )


def _verify_report_files(report: ResolveHostReport) -> None:
    for label, evidence in (
        ("app bundle", report.application.info_plist),
        ("script module", report.bridge.module),
        ("bridge library", report.bridge.library),
    ):
        path = Path(evidence.path)
        try:
            actual = sha256_file(path)
        except OSError as error:
            raise BridgeBindingError(f"missing host file ({label}): {path}") from error
        if actual != evidence.sha256:
            raise BridgeBindingError(f"host file drift since readiness report ({label}): {path}")


_loaded_modules: dict[str, ScriptModule] = {}


def reset_script_module_cache() -> None:
    """Drop cached bridge modules so a relaunched Resolve gets a fresh handle.

    A ``DaVinciResolveScript`` module loaded before the application quit
    keeps returning ``None`` from ``scriptapp`` afterwards; the wrapper
    replaces itself in ``sys.modules`` with the native ``fusionscript``
    module, so both entries must be dropped before a fresh load reconnects
    to the newly launched instance.
    """

    _loaded_modules.clear()
    sys.modules.pop("fusionscript", None)
    for name in [key for key in sys.modules if key.startswith("_fvp_bridge_")]:
        del sys.modules[name]


def load_script_module(report: ResolveHostReport) -> ScriptModule:
    _require_local_paths(report)
    _verify_report_files(report)
    key = f"{report.bridge.module.path}|{report.bridge.module.sha256}"
    cached = _loaded_modules.get(key)
    if cached is not None:
        return cached
    os.environ["RESOLVE_SCRIPT_LIB"] = report.bridge.library.path
    spec = importlib.util.spec_from_file_location(
        f"_fvp_bridge_{len(_loaded_modules)}", report.bridge.module.path
    )
    if spec is None or spec.loader is None:
        raise BridgeConnectionError(
            f"cannot load official script module: {report.bridge.module.path}"
        )
    wrapper = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(wrapper)
        inner = cast("ScriptModule | None", wrapper.script_module)
    except (OSError, AttributeError, ImportError) as error:
        raise BridgeConnectionError(f"official script module failed to load: {error}") from error
    if inner is None:
        raise BridgeConnectionError("official script module did not expose the scripting extension")
    _loaded_modules[key] = inner
    return inner


def _int_fields(values: list[int | str]) -> tuple[int, int, int, int] | None:
    if len(values) < MIN_VERSION_FIELDS:
        return None
    fields: list[int] = []
    for value in values[:MIN_VERSION_FIELDS]:
        if not isinstance(value, int):
            return None
        fields.append(value)
    first, second, third, fourth = fields
    return first, second, third, fourth


def _build_matches(core: str, report_build: str, live_build: int) -> bool:
    if report_build.isdigit() and int(report_build) == live_build:
        return True
    if f"{core}{live_build:04d}" == report_build:
        return True
    tail = report_build.removeprefix(core)
    return tail.isdigit() and int(tail) == live_build


def verify_version_binding(resolve: ResolveApi, report: ResolveHostReport) -> VersionBinding:
    product_name = resolve.GetProductName()
    if product_name not in ALLOWED_PRODUCTS:
        raise BridgeBindingError(f"unexpected Resolve product name: {product_name!r}")
    fields = _int_fields(resolve.GetVersion())
    if fields is None:
        raise BridgeBindingError("GetVersion() did not return four leading integer version fields")
    major, minor, patch, build = fields
    core = f"{major}.{minor}.{patch}"
    version_string = resolve.GetVersionString()
    mismatched = (
        core != report.application.version
        or not _build_matches(core, report.application.build, build)
        or not version_string.startswith(core)
    )
    if mismatched:
        raise BridgeBindingError(
            "bridge binding mismatch: "
            f"live {product_name} {version_string} vs report "
            f"{report.application.version} build {report.application.build}"
        )
    return VersionBinding(
        product_name=product_name,
        version_core=core,
        build_number=build,
        version_string=version_string,
    )


def connect_with_module(report: ResolveHostReport, module: ScriptModule) -> ResolveConnection:
    resolve = module.scriptapp("Resolve")
    if resolve is None:
        raise BridgeUnavailable("scriptapp('Resolve') returned None: scripting API unavailable")
    return ResolveConnection(resolve=resolve, binding=verify_version_binding(resolve, report))


def connect(report: ResolveHostReport) -> ResolveConnection:
    return connect_with_module(report, load_script_module(report))
