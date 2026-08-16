"""Offline fault scenarios for the base-cut spike (``QA_FAULT_FIXTURE`` CLI mode).

Fault specs drive the real build/readback/compare path against the in-memory
fakes from ``base_cut_fakes``, so wrong-media, off-by-one-frame, wrong-track,
and duplicate-append faults are demonstrably detected by readback comparison
without a live Resolve. Exit code is ``EXIT_FAULT`` whether or not the injected
fault was detected; the printed observation discriminates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from services.contracts.primitives import StrictModel
from services.fixtures.manifest import Phase0AFixtureManifest
from services.resolve_bridge.base_cut import build_base_cut
from services.resolve_bridge.base_cut_cli import MARKER
from services.resolve_bridge.base_cut_compare import compare
from services.resolve_bridge.base_cut_fakes import FakeBaseCutMediaPool
from services.resolve_bridge.base_cut_plan import (
    BaseCutError,
    expected_from_manifest,
    fixture_media_map,
    ir_from_manifest,
    request_from_ir,
)
from services.resolve_bridge.connection import (
    ProjectManagerApi,
    ResolveConnection,
    VersionBinding,
)

if TYPE_CHECKING:
    from services.resolve_bridge.lifecycle import TimelineApi

EXIT_FAULT = 2

FaultKind = Literal[
    "wrong_media_same_duration", "off_by_one_frame", "wrong_track", "duplicate_append"
]


class BaseCutFaultSpec(StrictModel):
    fault: FaultKind


def load_fault_spec(path: Path) -> BaseCutFaultSpec:
    return BaseCutFaultSpec.model_validate_json(path.read_bytes())


class FakeBaseCutProject:
    def __init__(self, name: str, pool: FakeBaseCutMediaPool) -> None:
        self._name = name
        self._pool = pool
        self._settings: dict[str, str] = {"timelineFrameRate": "24"}

    def GetName(self) -> str:
        return self._name

    def GetMediaPool(self) -> FakeBaseCutMediaPool:
        return self._pool

    def SetCurrentTimeline(self, timeline: object) -> bool:
        return True

    def GetTimelineCount(self) -> int:
        return 1 if self._pool.current_timeline() is not None else 0

    def GetTimelineByIndex(self, index: int) -> TimelineApi | None:
        timeline = self._pool.current_timeline()
        if index != 1 or timeline is None:
            return None
        return timeline

    def GetSetting(self, setting_name: str) -> str:
        return self._settings.get(setting_name, "")

    def SetSetting(self, setting_name: str, setting_value: str) -> bool:
        self._settings[setting_name] = setting_value
        return True


class FakeBaseCutManager:
    def __init__(self, fault: str) -> None:
        self._fault = fault
        self._names: set[str] = set()
        self._projects: dict[str, FakeBaseCutProject] = {}
        self._current: FakeBaseCutProject | None = None

    def CreateProject(self, project_name: str) -> FakeBaseCutProject:
        self._names.add(project_name)
        project = FakeBaseCutProject(project_name, FakeBaseCutMediaPool(self._fault))
        self._projects[project_name] = project
        self._current = project
        return project

    def LoadProject(self, project_name: str) -> FakeBaseCutProject | None:
        return self._projects.get(project_name)

    def GetCurrentProject(self) -> FakeBaseCutProject | None:
        return self._current

    def CloseProject(self, project: object) -> bool:
        self._current = None
        return True

    def DeleteProject(self, project_name: str) -> bool:
        if self._current is not None and self._current.GetName() == project_name:
            return False
        self._names.discard(project_name)
        self._projects.pop(project_name, None)
        return True

    def SaveProject(self) -> bool:
        return True

    def GetProjectListInCurrentFolder(self) -> list[str]:
        return sorted(self._names)


class FakeBaseCutResolve:
    def __init__(self, manager: FakeBaseCutManager) -> None:
        self._manager = manager

    def GetProductName(self) -> str:
        return "DaVinci Resolve Studio"

    def GetVersion(self) -> list[int | str]:
        return [21, 0, 4, 5, ""]

    def GetVersionString(self) -> str:
        return "21.0.4.5"

    def GetProjectManager(self) -> ProjectManagerApi:
        return self._manager


def build_fake_connection(fault: str) -> tuple[ResolveConnection, FakeBaseCutManager]:
    manager = FakeBaseCutManager(fault)
    connection = ResolveConnection(
        resolve=FakeBaseCutResolve(manager),
        binding=VersionBinding(
            product_name="DaVinci Resolve Studio",
            version_core="21.0.4",
            build_number=5,
            version_string="21.0.4.5",
        ),
    )
    return connection, manager


def run_fault_cli(spec_path: Path, manifest_path: Path, fixture_dir: Path) -> int:
    try:
        spec = load_fault_spec(spec_path)
        manifest = Phase0AFixtureManifest.model_validate_json(manifest_path.read_bytes())
        media = fixture_media_map(fixture_dir)
        request = request_from_ir(ir_from_manifest(manifest), media, require_files=False)
        expected = expected_from_manifest(manifest, media)
    except (OSError, ValidationError, BaseCutError) as error:
        print(f"{MARKER} fault-fixture invalid: {error}", file=sys.stderr)
        return EXIT_FAULT
    connection, manager = build_fake_connection(spec.fault)
    try:
        snapshot = build_base_cut(connection, request)
    except (BaseCutError, OSError) as error:
        print(f"{MARKER} mismatch code=api-error {error}", file=sys.stderr)
        return _cleanup_exit(manager)
    outcome = compare(expected, snapshot)
    for mismatch in outcome.mismatches:
        print(f"{MARKER} mismatch code={mismatch.code} {mismatch.detail}", file=sys.stderr)
    if outcome.passed:
        print("ERROR: injected fault was not detected by readback comparison", file=sys.stderr)
    return _cleanup_exit(manager)


def _cleanup_exit(manager: FakeBaseCutManager) -> int:
    leftover = [name for name in manager.GetProjectListInCurrentFolder() if name.startswith("__")]
    if leftover:
        print(f"ERROR: owned projects leaked: {leftover}", file=sys.stderr)
    return EXIT_FAULT
