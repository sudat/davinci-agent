from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
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


def _media_bin(report_path: Path, name: str, env_key: str) -> Path:
    override = os.environ.get(env_key)
    return Path(override) if override else report_path.parent / "bootstrap/ffmpeg-7.1.1/bin" / name


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


@dataclass
class LiveRun:
    result: subprocess.CompletedProcess[str]
    bundle: Path | None
    fixture_dir: Path


@pytest.fixture(scope="session")
def live_cli_run(request: pytest.FixtureRequest, live_connection: ResolveConnection) -> LiveRun:
    report_path = _report_path(request.config)
    assert report_path is not None
    fixture_dir = Path(
        os.environ.get(
            "FVP_FIXED_PRESENTATION_FIXTURE_DIR",
            str(report_path.parent / "phase-0a" / "fixture"),
        )
    )
    if not (fixture_dir / "source.mov").is_file() or not (fixture_dir / "subtitle.srt").is_file():
        pytest.skip(f"phase 0A fixture media not materialized: {fixture_dir}")
    ffmpeg = _media_bin(report_path, "ffmpeg", "FVP_FFMPEG_BIN")
    ffprobe = _media_bin(report_path, "ffprobe", "FVP_FFPROBE_BIN")
    if not (ffmpeg.is_file() and ffprobe.is_file()):
        pytest.skip(f"pinned ffmpeg/ffprobe binaries not found: {ffmpeg} / {ffprobe}")
    evidence_root = request.config.getoption("--resolve-evidence")
    bundle = Path(str(evidence_root)) / "fixed-presentation" if evidence_root else None
    render_dir = (bundle / "render") if bundle else fixture_dir.parent / "fp-render"
    argv = [
        sys.executable,
        "-m",
        "services.resolve_bridge.fixed_presentation",
        "--manifest",
        str(MANIFEST),
        "--fixture-dir",
        str(fixture_dir),
        "--report",
        str(report_path),
        "--render-dir",
        str(render_dir),
        "--ffmpeg",
        str(ffmpeg),
        "--ffprobe",
        str(ffprobe),
    ]
    if bundle is not None:
        argv += ["--evidence", str(bundle)]
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=900)
    return LiveRun(result=result, bundle=bundle, fixture_dir=fixture_dir)


@pytest.mark.resolve_live
def test_live_fixed_presentation_stdout_contract(live_cli_run: LiveRun) -> None:
    result = live_cli_run.result
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "needs-live" not in combined
    assert "fixed-presentation: PASS fixture=p0a-cfr30-fixed" in result.stdout
    assert "element=subtitle strategy=external status=verified" in result.stdout
    assert "placement=in-timeline:false" in result.stdout
    assert "rung=direct available=false" in result.stdout
    assert "rung=interchange available=false" in result.stdout
    assert "rung=template available=false" in result.stdout
    assert "subtitle-artifact paired=" in result.stdout
    for slate in ("intro-001", "outro-001"):
        assert f"element={slate} status=verified" in result.stdout
        assert "media-match=true" in result.stdout
    assert "element=audio-preset strategy=direct status=verified" in result.stdout
    assert "render status=complete frames=660" in result.stdout


@pytest.mark.resolve_live
def test_live_fixed_presentation_evidence_bundle(live_cli_run: LiveRun) -> None:
    bundle = live_cli_run.bundle
    if bundle is None:
        pytest.skip("no --resolve-evidence option; evidence bundle not requested")
    for name in (
        "ledger.jsonl",
        "head.json",
        "fixed-presentation-report.json",
        "strategy-table.json",
        "readback-table.json",
        "render-ffprobe.json",
        "render-sha256.txt",
    ):
        assert (bundle / name).is_file(), name
    rows = [json.loads(line) for line in (bundle / "ledger.jsonl").read_text().splitlines()]
    assert rows[-1]["event_type"] == "completion"
    assert rows[-1]["exit_code"] == 0
    report = json.loads((bundle / "fixed-presentation-report.json").read_text())
    assert report["passed"] is True
    assert report["base_cut_passed"] is True
    assert report["mismatches"] == []
    table = {row["element"]: row for row in report["strategy_table"]}
    assert table["subtitle"]["strategy"] == "external"
    assert table["subtitle"]["status"] == "verified"
    assert table["intro-outro"]["strategy"] == "direct"
    assert table["audio-preset"]["strategy"] == "direct"
    assert report["subtitle"]["in_timeline"] is False
    artifact = report["subtitle"]["artifact"]
    assert artifact is not None
    assert artifact["cue"]["text"] == "PHASE 0A FIXED SUBTITLE"
    assert artifact["packet_pts_seconds"] == 6.0
    assert artifact["packet_duration_seconds"] == 2.0
    assert Path(artifact["paired_path"]).is_file()
    rendered = Path(report["render"]["output_path"])
    assert rendered.is_file()
    assert hashlib.sha256(rendered.read_bytes()).hexdigest() == report["render"]["output_sha256"]
    strategy_text = (bundle / "strategy-table.json").read_text()
    assert '"strategy": "external"' in strategy_text


@pytest.mark.resolve_live
def test_live_fixed_presentation_leaves_no_owned_projects(
    live_connection: ResolveConnection,
) -> None:
    manager = live_connection.project_manager()
    owned = [
        name for name in manager.GetProjectListInCurrentFolder() if name.startswith(PROJECT_PREFIX)
    ]
    assert owned == [], f"fixed-presentation CLI leaked owned projects: {owned}"
