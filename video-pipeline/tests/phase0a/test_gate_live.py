from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.contracts.build_report import BuildReport0A
from services.contracts.primitives import RationalFrameRate
from services.fixtures.manifest import Phase0AFixtureManifest
from services.gates import GateResult
from services.resolve_bridge.base_cut_plan import expected_from_manifest
from services.resolve_bridge.build_report_fingerprint import (
    requested_placement,
    timeline_fingerprint,
)
from services.resolve_bridge.connection import (
    BridgeConnectionError,
    ResolveConnection,
    connect,
    reset_script_module_cache,
)
from services.resolve_bridge.fixed_presentation_models import FRAME_ORIGIN
from services.resolve_bridge.lifecycle import PROJECT_PREFIX, cleanup_owned_projects
from services.resolve_bridge.readiness import load_host_report
from services.spike.gate_models import (
    RECOVERY_NAME,
    RESTART_NAME,
    CapabilityMatrix,
    CapabilityProbes,
    RecoveryRecord,
    RestartRecord,
    run_report_path,
)
from services.spike.stop_rules import stop_marker_path

MANIFEST = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")
POLICY = Path("config/gates/phase-0a-v1.json")
GATE_TIMEOUT_SECONDS = 1500


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
    # the gate subprocess restarts Resolve, invalidating this pre-restart handle
    try:
        deleted = cleanup_owned_projects(connection.project_manager())
    except (TypeError, BridgeConnectionError):
        return
    if deleted:
        print(f"teardown deleted owned projects: {sorted(deleted)}")


@pytest.fixture(scope="session")
def post_gate_connection(request: pytest.FixtureRequest, gate_run: GateRun) -> ResolveConnection:
    report_path = _report_path(request.config)
    assert report_path is not None
    reset_script_module_cache()
    report = load_host_report(report_path)
    return connect(report)


@dataclass
class GateRun:
    result: subprocess.CompletedProcess[str]
    evidence: Path
    host_report: Path


@pytest.fixture(scope="session")
def gate_run(request: pytest.FixtureRequest, live_connection: ResolveConnection) -> GateRun:
    report_path = _report_path(request.config)
    assert report_path is not None
    evidence_root = request.config.getoption("--resolve-evidence")
    if not evidence_root:
        pytest.skip("--resolve-evidence is required for the full live gate run")
    evidence = Path(str(evidence_root)) / "phase-0a"
    argv = [
        sys.executable,
        "-m",
        "services.spike.run_gate",
        "phase-0a",
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
    return GateRun(result=result, evidence=evidence, host_report=report_path)


@pytest.mark.resolve_live
def test_live_gate_passes(gate_run: GateRun) -> None:
    combined = gate_run.result.stdout + gate_run.result.stderr
    assert gate_run.result.returncode == 0, combined[-4000:]
    assert "phase-0a: PASS" in gate_run.result.stdout
    assert "phase-0a: STOP" not in combined
    assert "needs-live" not in combined


@pytest.mark.resolve_live
def test_live_gate_result_artifact(gate_run: GateRun) -> None:
    result_path = gate_run.evidence / "gate-result.json"
    assert result_path.is_file()
    result = GateResult.model_validate_json(result_path.read_bytes())
    assert result.passed is True
    assert result.gate_id == "phase-0a"
    assert len(result.criteria_results) == 8
    assert all(row.passed for row in result.criteria_results)


@pytest.mark.resolve_live
def test_live_six_runs_share_expected_fingerprint(gate_run: GateRun) -> None:
    manifest = Phase0AFixtureManifest.model_validate_json(MANIFEST.read_bytes())
    fixture_dir = gate_run.host_report.parent / "phase-0a" / "fixture"
    expected = expected_from_manifest(
        manifest, {sid: str(fixture_dir / name) for sid, name in (
            ("source", "source.mov"), ("intro", "intro.mov"), ("outro", "outro.mov")
        )}
    )
    rate = RationalFrameRate(
        num=manifest.recipe.source.frame_rate.num, den=manifest.recipe.source.frame_rate.den
    )
    expected_fp = timeline_fingerprint(
        tuple(requested_placement(want, rate, FRAME_ORIGIN) for want in expected.items)
    )
    fingerprints = set()
    for index in range(1, 7):
        report = BuildReport0A.model_validate_json(
            run_report_path(gate_run.evidence, index).read_bytes()
        )
        fingerprints.add(timeline_fingerprint(tuple(row.observed for row in report.items)))
        assert all(row.observed == row.requested for row in report.items)
    assert len(fingerprints) == 1
    assert fingerprints.pop() == expected_fp


@pytest.mark.resolve_live
def test_live_restart_evidence(gate_run: GateRun) -> None:
    record = RestartRecord.model_validate_json(
        (gate_run.evidence / "restart" / RESTART_NAME).read_bytes()
    )
    assert record.error == ""
    assert record.before == record.after
    assert record.before_pid is not None
    assert record.after_pid is not None
    assert record.before_pid != record.after_pid


@pytest.mark.resolve_live
def test_live_recovery_evidence(gate_run: GateRun) -> None:
    record = RecoveryRecord.model_validate_json(
        (gate_run.evidence / "recovery" / RECOVERY_NAME).read_bytes()
    )
    assert record.rebuild_project_name != record.partial.project_name
    assert record.rebuild_loaded_partial is False
    assert record.partial_reread_items == record.partial.items_placed
    assert record.partial_reread_fingerprint == record.partial.partial_fingerprint
    assert record.partial_deleted is True
    rebuild = BuildReport0A.model_validate_json(
        (gate_run.evidence / record.rebuild_report_rel).read_bytes()
    )
    assert all(row.observed == row.requested for row in rebuild.items)


@pytest.mark.resolve_live
def test_live_capability_matrix_recorded(gate_run: GateRun) -> None:
    matrix_path = gate_run.evidence / "capability-matrix.json"
    assert matrix_path.is_file()
    probes = CapabilityProbes.model_validate_json(
        (gate_run.evidence / "probes" / "capability-probes.json").read_bytes()
    )
    assert probes.frame_origin == 108000
    assert probes.probe_record_start >= probes.frame_origin
    matrix = CapabilityMatrix.model_validate_json(matrix_path.read_bytes())
    assert len(matrix.capabilities) == 5
    assert all(entry.live_verified and entry.api_available for entry in matrix.capabilities)
    assert len(matrix.findings) >= 4
    published = (
        Path("capabilities") / f"resolve-{matrix.resolve_version}" / "capability-matrix.json"
    )
    assert published.is_file()


@pytest.mark.resolve_live
def test_live_no_stop_marker_and_no_owned_projects(
    gate_run: GateRun, post_gate_connection: ResolveConnection
) -> None:
    assert not stop_marker_path(gate_run.evidence).exists()
    manager = post_gate_connection.project_manager()
    owned = [
        name
        for name in manager.GetProjectListInCurrentFolder()
        if name.startswith(PROJECT_PREFIX)
    ]
    assert owned == [], f"live gate leaked owned projects: {owned}"
