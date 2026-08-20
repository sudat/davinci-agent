"""Todo-62 LIVE integration: the real A/B builds through the bridge.

One session-scoped run of the REAL phase-3 gate CLI drives both snapshot
builds (same approved fixture Timeline IR under brand A then brand B),
reruns the complete Phase-2 gate, recomputes all four criteria, and leaves
Resolve running while deleting only the owned ``__fvp_test__`` projects.
Every assertion reads the measured evidence tree, never the driver log
alone.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.gates import GateResult
from services.resolve_bridge.connection import connect
from services.resolve_bridge.lifecycle import PROJECT_PREFIX
from services.resolve_bridge.readiness import load_host_report

ATTEMPT = Path(
    "/Users/stc/Developer/davinci-agent/.omo/start-work/attempts/"
    "0d13f6a4397e3f032d918760cb1708dffa523c6db975a8267d511b103b0e4b75"
)
EVIDENCE = ATTEMPT / "phase-3-live"


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


@dataclass(frozen=True, slots=True)
class LiveGateRun:
    process: subprocess.CompletedProcess[str]
    evidence: Path


@pytest.fixture(scope="module")
def gate_run(request: pytest.FixtureRequest) -> Iterator[LiveGateRun]:
    report = _report_path(request.config)
    if report is None:
        pytest.skip("resolve host report not found: pass --resolve-evidence")
    evidence_option = request.config.getoption("--resolve-evidence")
    if not evidence_option:
        pytest.skip("live gate requires --resolve-evidence")
    if request.config.getoption("--exclusive-resolve-lease"):
        lock_path = Path(str(evidence_option)) / "phase-3-gate-lease.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            pytest.skip("exclusive resolve lease held by another session")
        print(f"exclusive resolve lease acquired: {lock_path}")
        try:
            yield _run_gate(report)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        return
    yield _run_gate(report)


def _run_gate(report: Path) -> LiveGateRun:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    argv = (
        sys.executable,
        "-m",
        "services.job_runner.run_gate",
        "phase-3",
        "--policy",
        "config/gates/phase-3-v1.json",
        "--evidence",
        str(EVIDENCE),
    )
    environment = dict(os.environ)
    environment["RESOLVE_HOST_REPORT"] = str(report)
    process = subprocess.run(
        argv,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=7200,
    )
    return LiveGateRun(process=process, evidence=EVIDENCE)


@pytest.mark.resolve_live
def test_live_gate_passes(gate_run: LiveGateRun) -> None:
    combined = gate_run.process.stdout + gate_run.process.stderr
    assert gate_run.process.returncode == 0, combined
    assert "phase3-gate PASS gate-result=" in gate_run.process.stdout
    assert "manual_finalization=false phase4_dependency=false" in gate_run.process.stdout
    result = GateResult.model_validate_json(
        (gate_run.evidence / "gate-result.json").read_bytes()
    )
    assert result.passed is True
    assert all(row.passed for row in result.criteria_results)


@pytest.mark.resolve_live
def test_live_ab_structure_identical_presentation_differs(gate_run: LiveGateRun) -> None:
    observation = json.loads((gate_run.evidence / "observation.json").read_bytes())
    builds = {build["snapshot_id"]: build for build in observation["builds"]}

    def structure_keys(snapshot: str) -> list[tuple[object, ...]]:
        return [
            (
                row["item_id"],
                row["source_start"],
                row["source_end"],
                row["record_start"],
                row["record_end"],
            )
            for row in builds[snapshot]["structure"]
        ]

    keys_a = structure_keys("p3-brand-a")
    keys_b = structure_keys("p3-brand-b")
    assert keys_a == keys_b
    assert len(keys_a) > 0
    hashes_a = builds["p3-brand-a"]["presentation"]
    hashes_b = builds["p3-brand-b"]["presentation"]
    assert hashes_a["manifest_sha256"] != hashes_b["manifest_sha256"]
    assert set(hashes_a["asset_sha256"]) == set(hashes_b["asset_sha256"])
    for kind in hashes_a["asset_sha256"]:
        assert hashes_a["asset_sha256"][kind] != hashes_b["asset_sha256"][kind]


@pytest.mark.resolve_live
def test_live_regression_rerun_is_recorded_and_passed(gate_run: LiveGateRun) -> None:
    regression = json.loads((gate_run.evidence / "observation.json").read_bytes())[
        "regression"
    ]
    assert regression["exit_code"] == 0
    assert regression["passed"] is True
    rerun = GateResult.model_validate_json(
        Path(regression["result_path"]).read_bytes()
    )
    assert rerun.gate_id == "phase-2"
    assert rerun.passed is True
    assert all(row.passed for row in rerun.criteria_results)


@pytest.mark.resolve_live
def test_live_cleanup_leaves_no_owned_projects(
    gate_run: LiveGateRun, request: pytest.FixtureRequest
) -> None:
    report = _report_path(request.config)
    if report is None:
        pytest.skip("cleanup probe requires a resolve host report")
    connection = connect(load_host_report(report))
    manager = connection.project_manager()
    owned = [
        name
        for name in manager.GetProjectListInCurrentFolder()
        if name.startswith(PROJECT_PREFIX)
    ]
    assert owned == [], f"phase-3 gate leaked owned projects: {owned}"
