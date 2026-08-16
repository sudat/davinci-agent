"""Disposable test-project lifecycle in the Resolve Project Manager.

Every mutation is confined to the ``__fvp_test__`` namespace; non-owned
resources are refused, and every mutation is confirmed against observable
project state instead of trusting API return codes alone.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from services.resolve_bridge.connection import (
    BridgeConnectionError,
    ProjectApi,
    ProjectManagerApi,
    ResolveConnection,
    TimelineApi,
    connect,
)
from services.resolve_bridge.evidence_recorder import CliRunRecord, record_cli_run
from services.resolve_bridge.readiness import load_host_report

PROJECT_PREFIX: Final = "__fvp_test__"
TIMELINE_PREFIX: Final = "__fvp_test__tl__"

EXIT_FAULT = 2
EXIT_REFUSED = 3
EXIT_UNAVAILABLE = 4


class LifecycleError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class NonOwnedResourceError(LifecycleError):
    """An operation targeted a resource outside the disposable namespace."""


def is_owned_project(name: str) -> bool:
    return name.startswith(PROJECT_PREFIX)


def is_owned_timeline(name: str) -> bool:
    return name.startswith(TIMELINE_PREFIX)


def owned_project_name() -> str:
    return f"{PROJECT_PREFIX}{uuid.uuid4().hex}"


def owned_timeline_name() -> str:
    return f"{TIMELINE_PREFIX}{uuid.uuid4().hex}"


def project_names(manager: ProjectManagerApi) -> frozenset[str]:
    return frozenset(manager.GetProjectListInCurrentFolder())


def create_disposable_project(manager: ProjectManagerApi, name: str) -> ProjectApi:
    if not is_owned_project(name):
        raise NonOwnedResourceError(f"refusing non-owned project creation: {name}")
    created = manager.CreateProject(name)
    if created is None:
        raise LifecycleError(f"CreateProject failed: {name}")
    if created.GetName() != name:
        raise LifecycleError(f"created project name mismatch: {created.GetName()} != {name}")
    if not manager.SaveProject():
        raise LifecycleError(f"SaveProject failed: {name}")
    if name not in project_names(manager):
        raise LifecycleError(f"project creation not observable in project list: {name}")
    return created


def create_owned_timeline(project: ProjectApi, name: str) -> TimelineApi:
    if not is_owned_timeline(name):
        raise NonOwnedResourceError(f"refusing non-owned timeline creation: {name}")
    pool = project.GetMediaPool()
    if pool is None:
        raise LifecycleError("media pool unavailable")
    timeline = pool.CreateEmptyTimeline(name)
    if timeline is None:
        raise LifecycleError(f"timeline creation failed: {name}")
    if not project.SetCurrentTimeline(timeline):
        raise LifecycleError(f"SetCurrentTimeline failed: {name}")
    if not _timeline_present(project, name):
        raise LifecycleError(f"timeline not observable via project readback: {name}")
    return timeline


def _timeline_present(project: ProjectApi, name: str) -> bool:
    for index in range(1, project.GetTimelineCount() + 1):
        candidate = project.GetTimelineByIndex(index)
        if candidate is not None and candidate.GetName() == name:
            return True
    return False


def delete_owned_project(manager: ProjectManagerApi, name: str) -> None:
    if not is_owned_project(name):
        raise NonOwnedResourceError(f"refusing non-owned project deletion: {name}")
    current = manager.GetCurrentProject()
    if current is not None and current.GetName() == name and not manager.CloseProject(current):
        raise LifecycleError(f"CloseProject failed for loaded disposable project: {name}")
    if name in project_names(manager) and not manager.DeleteProject(name):
        raise LifecycleError(f"DeleteProject failed: {name}")
    if name in project_names(manager):
        raise LifecycleError(f"deletion not observable in project list: {name}")


def owned_project_names(manager: ProjectManagerApi) -> tuple[str, ...]:
    return tuple(sorted(name for name in project_names(manager) if is_owned_project(name)))


def cleanup_owned_projects(manager: ProjectManagerApi) -> tuple[str, ...]:
    deleted: list[str] = []
    for name in owned_project_names(manager):
        try:
            delete_owned_project(manager, name)
        except LifecycleError as error:
            print(f"cleanup failed for {name}: {error}", file=sys.stderr)
            continue
        deleted.append(name)
    return tuple(deleted)


def _assert_only_owned_changes(before: frozenset[str], current: frozenset[str], owned: str) -> None:
    added = current - before
    removed = before - current
    if removed or added != {owned}:
        raise LifecycleError(
            f"non-owned project-list changes detected: +{sorted(added)} -{sorted(removed)}"
        )


@dataclass(frozen=True, slots=True)
class ExerciseResult:
    project_name: str
    timeline_name: str


def run_exercise(connection: ResolveConnection) -> ExerciseResult:
    manager = connection.project_manager()
    before = project_names(manager)
    name = owned_project_name()
    timeline_name = owned_timeline_name()
    try:
        project = create_disposable_project(manager, name)
        create_owned_timeline(project, timeline_name)
        _assert_only_owned_changes(before, project_names(manager), name)
        delete_owned_project(manager, name)
        after = project_names(manager)
        if after != before:
            raise LifecycleError(
                f"project list did not return to baseline after deletion: "
                f"+{sorted(after - before)} -{sorted(before - after)}"
            )
    finally:
        cleanup_owned_projects(manager)
    return ExerciseResult(project_name=name, timeline_name=timeline_name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    parser.add_argument("--evidence", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    fault_fixture = os.environ.get("QA_FAULT_FIXTURE")
    if fault_fixture is not None:
        from services.resolve_bridge.faults import run_fault_cli  # noqa: PLC0415

        return run_fault_cli(Path(fault_fixture))
    if arguments.report is None:
        print("--report is required outside fault mode", file=sys.stderr)
        return 2
    argv = [sys.executable, "-m", "services.resolve_bridge.lifecycle", *sys.argv[1:]]
    try:
        report = load_host_report(arguments.report)
        connection = connect(report)
        binding = connection.binding
        result = run_exercise(connection)
    except BridgeConnectionError as error:
        print(f"bridge unavailable: {error}", file=sys.stderr)
        if arguments.evidence is not None:
            record_cli_run(
                arguments.evidence,
                CliRunRecord(
                    argv=argv,
                    exit_code=EXIT_UNAVAILABLE,
                    expected_exit=EXIT_UNAVAILABLE,
                    stdout="",
                    stderr=f"{error}\n",
                    observe="lifecycle-ok:",
                ),
            )
        return EXIT_UNAVAILABLE
    except (LifecycleError, OSError) as error:
        print(f"lifecycle failed: {error}", file=sys.stderr)
        if arguments.evidence is not None:
            record_cli_run(
                arguments.evidence,
                CliRunRecord(
                    argv=argv,
                    exit_code=1,
                    expected_exit=1,
                    stdout="",
                    stderr=f"{error}\n",
                    observe="lifecycle-ok:",
                ),
            )
        return 1
    stdout = (
        f"connected: {binding.product_name} {binding.version_core} build {binding.build_number} "
        f"({binding.version_string})\n"
        f"lifecycle-ok: project={result.project_name} timeline={result.timeline_name} "
        "only-owned-changes=true\n"
    )
    print(stdout, end="")
    if arguments.evidence is not None:
        record_cli_run(
            arguments.evidence,
            CliRunRecord(
                argv=argv,
                exit_code=0,
                expected_exit=0,
                stdout=stdout,
                stderr="",
                observe="lifecycle-ok:",
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
