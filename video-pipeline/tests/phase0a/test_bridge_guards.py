from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from services.resolve_bridge import faults
from services.resolve_bridge.connection import (
    BridgeBindingError,
    BridgeUnavailable,
    NonLocalEndpointError,
    connect_with_module,
    load_script_module,
)
from services.resolve_bridge.faults import FakeProjectManager, FaultSpec
from services.resolve_bridge.lifecycle import (
    PROJECT_PREFIX,
    NonOwnedResourceError,
    cleanup_owned_projects,
    delete_owned_project,
    is_owned_project,
    owned_project_name,
)

FAULTS = Path("tests/fixtures/resolve-bridge-faults")


def test_owned_project_namespace_is_disposable_and_guarded() -> None:
    name = owned_project_name()
    assert name.startswith(PROJECT_PREFIX)
    assert is_owned_project(name)
    assert not is_owned_project("OwnerMain")


def test_wrong_version_or_wrong_build_fails_closed() -> None:
    for live_version in ([21, 0, 3, 3, ""], [21, 0, 4, 9, ""]):
        scenario = faults.build_scenario(FaultSpec(fault="wrong_build", live_version=live_version))
        assert scenario.module is not None
        with pytest.raises(BridgeBindingError, match="binding mismatch"):
            connect_with_module(scenario.report, scenario.module)


def test_remote_endpoint_refused_before_any_load() -> None:
    scenario = faults.build_scenario(
        FaultSpec(
            fault="remote_endpoint",
            library_path="https://198.51.100.7/fusionscript.so",
            module_path="https://198.51.100.7/DaVinciResolveScript.py",
        )
    )
    with pytest.raises(NonLocalEndpointError, match="refusing non-local bridge endpoint"):
        load_script_module(scenario.report)


def test_missing_api_object_fails_closed() -> None:
    scenario = faults.build_scenario(FaultSpec(fault="missing_api"))
    assert scenario.module is not None
    with pytest.raises(BridgeUnavailable, match="scripting API unavailable"):
        connect_with_module(scenario.report, scenario.module)


def test_non_owned_project_deletion_refused() -> None:
    scenario = faults.build_scenario(
        FaultSpec(fault="non_owned_delete", protected_project="OwnerMain")
    )
    assert scenario.resolve is not None
    manager = scenario.resolve.GetProjectManager()
    with pytest.raises(NonOwnedResourceError, match="refusing non-owned project deletion"):
        delete_owned_project(manager, "OwnerMain")
    assert "OwnerMain" in manager.GetProjectListInCurrentFolder()


def test_cleanup_deletes_only_owned_projects() -> None:
    manager = FakeProjectManager(names={"__fvp_test__stale", "OwnerMain"})
    deleted = cleanup_owned_projects(manager)
    assert deleted == ("__fvp_test__stale",)
    assert set(manager.GetProjectListInCurrentFolder()) == {"OwnerMain"}


def test_cli_fault_mode_reports_refused_non_owned_deletion() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "services.resolve_bridge.lifecycle"],
        env=os.environ | {"QA_FAULT_FIXTURE": str(FAULTS / "non-owned-delete.json")},
        check=False,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 3, combined
    assert "refusing non-owned project deletion: OwnerMain" in combined
    assert "protected-project-intact: OwnerMain" in combined
