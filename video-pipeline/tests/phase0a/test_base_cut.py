from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from services.resolve_bridge.connection import (
    BridgeConnectionError,
    ResolveConnection,
    connect,
)
from services.resolve_bridge.lifecycle import PROJECT_PREFIX, cleanup_owned_projects
from services.resolve_bridge.readiness import load_host_report

MANIFEST = Path("tests/fixtures/manifests/phase-0a/p0a-cfr30-fixed.json")


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
    deleted = cleanup_owned_projects(connection.project_manager())
    if deleted:
        print(f"teardown deleted owned projects: {sorted(deleted)}")


@pytest.mark.resolve_live
def test_live_base_cut_exact_placement_with_evidence(
    request: pytest.FixtureRequest, live_connection: ResolveConnection
) -> None:
    report_path = _report_path(request.config)
    assert report_path is not None
    fixture_dir = Path(
        os.environ.get("FVP_BASE_CUT_FIXTURE_DIR", str(report_path.parent / "phase-0a" / "fixture"))
    )
    if not (fixture_dir / "source.mov").is_file():
        pytest.skip(f"phase 0A fixture media not materialized: {fixture_dir}")
    evidence_root = request.config.getoption("--resolve-evidence")
    bundle = Path(str(evidence_root)) / "base-cut" if evidence_root else None
    argv = [
        sys.executable,
        "-m",
        "services.resolve_bridge.base_cut",
        "--manifest",
        str(MANIFEST),
        "--fixture-dir",
        str(fixture_dir),
        "--report",
        str(report_path),
    ]
    if bundle is not None:
        argv += ["--evidence", str(bundle)]
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "base-cut: PASS" in result.stdout
    assert "requested=built=true" in result.stdout
    assert "media-match=true" in result.stdout
    assert "track-match=true" in result.stdout
    assert "link-match=true" in result.stdout
    assert "source-delta=0" in result.stdout
    assert "record-delta=0" in result.stdout
    assert "deltas=0" in result.stdout
    for row in ("intro-001", "cut-001", "cut-002", "outro-001"):
        assert f"item={row} kind=video" in result.stdout
        assert f"item={row} kind=audio" in result.stdout
    assert "needs-live" not in result.stdout
    if bundle is not None:
        assert (bundle / "ledger.jsonl").is_file()
        assert (bundle / "head.json").is_file()
        assert (bundle / "base-cut-report.json").is_file()
        rows = [json.loads(line) for line in (bundle / "ledger.jsonl").read_text().splitlines()]
        assert rows[-1]["event_type"] == "completion"
        assert rows[-1]["exit_code"] == 0
        report = json.loads((bundle / "base-cut-report.json").read_text())
        assert report["outcome"]["passed"] is True
        assert report["outcome"]["max_source_delta"] == 0
        assert report["outcome"]["max_record_delta"] == 0
        assert len(report["built"]["items"]) == 8
        assert report["built"]["record_frame_count"] == 660


@pytest.mark.resolve_live
def test_live_base_cut_cli_leaves_no_owned_projects(
    request: pytest.FixtureRequest, live_connection: ResolveConnection
) -> None:
    manager = live_connection.project_manager()
    names = manager.GetProjectListInCurrentFolder()
    owned = [name for name in names if name.startswith(PROJECT_PREFIX)]
    assert owned == [], f"base-cut CLI leaked owned projects: {owned}"
