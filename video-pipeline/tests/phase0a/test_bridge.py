from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from services.resolve_bridge.connection import (
    ALLOWED_PRODUCTS,
    BridgeConnectionError,
    ResolveConnection,
    connect,
)
from services.resolve_bridge.launch import (
    OWNER_INSTRUCTIONS,
    BridgeLaunchBlocked,
    launch_and_connect,
)
from services.resolve_bridge.lifecycle import PROJECT_PREFIX, cleanup_owned_projects
from services.resolve_bridge.readiness import load_host_report


def _report_path(config: pytest.Config) -> Path | None:
    override = os.environ.get("RESOLVE_HOST_REPORT")
    if override:
        return Path(override)
    evidence = config.getoption("--resolve-evidence")
    if evidence:
        candidate = Path(str(evidence)).resolve().parent / "resolve-host.json"
        if candidate.is_file():
            return candidate
    return None


def _launch_selected(config: pytest.Config) -> bool:
    markexpr = str(config.getoption("markexpr") or "")
    return "resolve_live" in markexpr and "not resolve_live" not in markexpr


@pytest.fixture(scope="session")
def live_connection(request: pytest.FixtureRequest) -> Iterator[ResolveConnection]:
    report_path = _report_path(request.config)
    if report_path is None:
        pytest.skip(
            "resolve host report not found: pass --resolve-evidence or set RESOLVE_HOST_REPORT"
        )
    report = load_host_report(report_path)
    try:
        if _launch_selected(request.config):
            connection = launch_and_connect(report)
        else:
            connection = connect(report)
    except BridgeLaunchBlocked as error:
        pytest.skip(
            f"Resolve not reachable: {error.detail} | owner instructions: {OWNER_INSTRUCTIONS}"
        )
    except BridgeConnectionError as error:
        pytest.skip(f"Resolve not reachable: {error.detail}")
    yield connection
    deleted = cleanup_owned_projects(connection.project_manager())
    if deleted:
        print(f"teardown deleted owned projects: {sorted(deleted)}")


@pytest.mark.resolve_live
def test_live_version_binding_matches_report(
    request: pytest.FixtureRequest, live_connection: ResolveConnection
) -> None:
    report_path = _report_path(request.config)
    assert report_path is not None
    report = load_host_report(report_path)
    binding = live_connection.binding
    assert binding.product_name in ALLOWED_PRODUCTS
    assert binding.version_core == report.application.version
    assert binding.version_string.startswith(binding.version_core)


@pytest.mark.resolve_live
def test_live_disposable_lifecycle_with_evidence(
    request: pytest.FixtureRequest, live_connection: ResolveConnection, tmp_path: Path
) -> None:
    report_path = _report_path(request.config)
    assert report_path is not None
    evidence_root = request.config.getoption("--resolve-evidence")
    bundle = Path(str(evidence_root)) / "lifecycle" if evidence_root else tmp_path / "lifecycle"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.resolve_bridge.lifecycle",
            "--report",
            str(report_path),
            "--evidence",
            str(bundle),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "connected:" in result.stdout
    assert "lifecycle-ok:" in result.stdout
    assert "only-owned-changes=true" in result.stdout
    project = _stdout_field(result.stdout, "project=")
    assert project.startswith(PROJECT_PREFIX)
    assert (bundle / "ledger.jsonl").is_file()
    assert (bundle / "head.json").is_file()
    rows = [json.loads(line) for line in (bundle / "ledger.jsonl").read_text().splitlines()]
    assert len(rows) >= 2
    assert [row["event_type"] for row in rows] == [
        "attempt" if index % 2 == 0 else "completion" for index in range(len(rows))
    ]
    assert rows[-1]["event_type"] == "completion"
    assert rows[-1]["exit_code"] == 0


def _stdout_field(stdout: str, marker: str) -> str:
    for line in stdout.splitlines():
        index = line.find(marker)
        if index >= 0:
            tail = line[index + len(marker) :]
            return tail.split()[0]
    raise AssertionError(f"marker {marker!r} not found in stdout")
