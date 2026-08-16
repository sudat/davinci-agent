"""Live Phase-0B gate tests (todo-25, ``resolve_live`` marker).

Runs the real gate CLI once per session against a disposable evidence tree,
then asserts the gate result, per-variant readbacks, sync measurements,
capability findings, stop-marker absence, and owned-project cleanup. Skips
with an explicit reason whenever the live bridge or its bindings are absent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.foundation_io import sha256_file
from services.gates import GateResult
from services.resolve_bridge.connection import (
    BridgeConnectionError,
    ResolveConnection,
    connect,
)
from services.resolve_bridge.lifecycle import PROJECT_PREFIX, cleanup_owned_projects
from services.resolve_bridge.readiness import load_host_report
from services.spike.gate_models import CapabilityMatrix
from services.spike.gate_phase0b_models import (
    RESULT_NAME,
    SYNC_NAME,
    LiveReadbackReport,
    SyncMeasurements,
    readback_report_path,
)
from services.spike.stop_rules import stop_marker_path

POLICY = Path("config/gates/phase-0b-v1.json")
GATE_TIMEOUT_SECONDS = 1800


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


@pytest.fixture(scope="session")
def live_connection(request: pytest.FixtureRequest) -> Iterator[ResolveConnection]:
    report_path = _report_path(request.config)
    if report_path is None:
        pytest.skip(
            "resolve host report not found: pass --resolve-evidence or set RESOLVE_HOST_REPORT"
        )
    report = load_host_report(report_path)
    try:
        connection = connect(report)
    except BridgeConnectionError as error:
        pytest.skip(f"Resolve not reachable: {error}")
    yield connection
    try:
        deleted = cleanup_owned_projects(connection.project_manager())
    except (TypeError, BridgeConnectionError):
        return
    if deleted:
        print(f"teardown deleted owned projects: {sorted(deleted)}")


@dataclass
class GateRun0b:
    result: subprocess.CompletedProcess[str]
    evidence: Path
    host_report: Path


@pytest.fixture(scope="session")
def gate_run(request: pytest.FixtureRequest, live_connection: ResolveConnection) -> GateRun0b:
    report_path = _report_path(request.config)
    assert report_path is not None
    evidence_root = request.config.getoption("--resolve-evidence")
    if not evidence_root:
        pytest.skip("--resolve-evidence is required for the full live gate run")
    evidence = Path(str(evidence_root)) / "phase-0b"
    argv = [
        sys.executable,
        "-m",
        "services.spike.run_gate",
        "phase-0b",
        "--policy",
        str(POLICY),
        "--evidence",
        str(evidence),
        "--host-report",
        str(report_path),
    ]
    result = subprocess.run(
        argv,
        env=os.environ | {"RESOLVE_HOST_REPORT": str(report_path)},
        check=False,
        capture_output=True,
        text=True,
        timeout=GATE_TIMEOUT_SECONDS,
    )
    return GateRun0b(result=result, evidence=evidence, host_report=report_path)


@pytest.mark.resolve_live
def test_live_gate_passes(gate_run: GateRun0b) -> None:
    combined = gate_run.result.stdout + gate_run.result.stderr
    assert gate_run.result.returncode == 0, combined[-4000:]
    assert "phase-0b: PASS" in gate_run.result.stdout
    assert "phase-0b: STOP" not in combined
    assert "needs-live" not in combined


@pytest.mark.resolve_live
def test_live_gate_result_artifact(gate_run: GateRun0b) -> None:
    result_path = gate_run.evidence / RESULT_NAME
    assert result_path.is_file()
    result = GateResult.model_validate_json(result_path.read_bytes())
    assert result.passed is True
    assert result.gate_id == "phase-0b"
    assert len(result.criteria_results) == 5
    assert all(row.passed for row in result.criteria_results)


@pytest.mark.resolve_live
def test_live_readback_exact_for_every_variant(gate_run: GateRun0b) -> None:
    for variant in (
        "p0b-cfr24",
        "p0b-ntsc2997",
        "p0b-ntsc5994",
        "p0b-vfr-2-3-cadence",
        "p0b-rotate90",
        "p0b-audio-offset1024",
    ):
        report = LiveReadbackReport.model_validate_json(
            readback_report_path(gate_run.evidence, variant).read_bytes()
        )
        assert report.max_frame_delta == 0, variant
        assert report.frame_origin == 108000
        assert report.bindings.host_report_sha256 == _host_sha(gate_run.host_report)


@pytest.mark.resolve_live
def test_live_sync_measurements_all_pass(gate_run: GateRun0b) -> None:
    sync = SyncMeasurements.model_validate_json(
        (gate_run.evidence / SYNC_NAME).read_bytes()
    )
    assert sync.all_passed is True
    assert {row.variant for row in sync.rows} >= {
        "p0b-cfr24",
        "p0b-ntsc2997",
        "p0b-ntsc5994",
        "p0b-vfr-2-3-cadence",
        "p0b-rotate90",
        "p0b-audio-offset1024",
    }
    assert any(row.kind == "tolerance-one-frame" for row in sync.rows)


@pytest.mark.resolve_live
def test_live_capability_findings_published(gate_run: GateRun0b) -> None:

    matrix_path = gate_run.evidence / "capability-matrix-0b.json"
    assert matrix_path.is_file()
    matrix = CapabilityMatrix.model_validate_json(matrix_path.read_bytes())
    assert matrix.findings
    assert all(finding.live_verified for finding in matrix.findings)
    published = (
        Path("capabilities") / f"resolve-{matrix.resolve_version}" / "capability-matrix.json"
    )
    assert published.is_file()
    published_matrix = CapabilityMatrix.model_validate_json(published.read_bytes())
    finding_ids = {finding.finding for finding in published_matrix.findings}
    assert {"edit-source-anchor-frame-readback"} <= finding_ids


@pytest.mark.resolve_live
def test_live_no_stop_marker_and_no_owned_projects(
    gate_run: GateRun0b, live_connection: ResolveConnection
) -> None:
    assert not stop_marker_path(gate_run.evidence).exists()
    manager = live_connection.project_manager()
    owned = [
        name
        for name in manager.GetProjectListInCurrentFolder()
        if name.startswith(PROJECT_PREFIX)
    ]
    assert owned == [], f"live gate leaked owned projects: {owned}"


def _host_sha(report: Path) -> str:
    return sha256_file(report)
