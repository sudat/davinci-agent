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

from services.contracts.build_report import BuildReport0A
from services.contracts.primitives import RationalFrameRate
from services.fixtures.manifest import Phase0AFixtureManifest
from services.resolve_bridge.base_cut_plan import expected_from_manifest
from services.resolve_bridge.build_report_fingerprint import (
    requested_placement,
    timeline_fingerprint,
)
from services.resolve_bridge.connection import (
    BridgeConnectionError,
    ResolveConnection,
    connect,
)
from services.resolve_bridge.fixed_presentation_models import FRAME_ORIGIN
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
    try:
        deleted = cleanup_owned_projects(connection.project_manager())
    except (TypeError, BridgeConnectionError):
        return
    if deleted:
        print(f"teardown deleted owned projects: {sorted(deleted)}")


@dataclass
class LiveRun:
    result: subprocess.CompletedProcess[str]
    bundle: Path | None
    report_path: Path | None
    host_report: Path
    ffmpeg: Path
    ffprobe: Path


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
    bundle = Path(str(evidence_root)) / "build-report" if evidence_root else None
    out = (bundle / "build-report.json") if bundle else fixture_dir.parent / "build-report.json"
    render_dir = (bundle / "render") if bundle else fixture_dir.parent / "br-render"
    argv = [
        sys.executable,
        "-m",
        "services.resolve_bridge.build_report",
        "--manifest",
        str(MANIFEST),
        "--fixture-dir",
        str(fixture_dir),
        "--host-report",
        str(report_path),
        "--out",
        str(out),
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
    return LiveRun(
        result=result,
        bundle=bundle,
        report_path=out if out.is_file() else None,
        host_report=report_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )


@pytest.mark.resolve_live
def test_live_build_report_stdout_contract(live_cli_run: LiveRun) -> None:
    result = live_cli_run.result
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "needs-live" not in combined
    assert "build-report: PASS fixture=p0a-cfr30-fixed" in result.stdout
    assert "build-report: items=8" in result.stdout
    assert "build-report: render job=" in result.stdout
    assert "completion=CompletionPercentage" in result.stdout
    assert "build-report: render output=" in result.stdout
    assert "decode-exit=0" in result.stdout
    assert "build-report: binding resolve=21.0.4" in result.stdout
    assert "code=strategy-subtitle-external" in result.stdout


@pytest.mark.resolve_live
def test_live_build_report_recomputes_items_and_render(live_cli_run: LiveRun) -> None:
    if live_cli_run.report_path is None:
        pytest.skip("no report artifact written; run with --resolve-evidence")
    report = BuildReport0A.model_validate_json(live_cli_run.report_path.read_bytes())
    manifest = Phase0AFixtureManifest.model_validate_json(MANIFEST.read_bytes())
    media = {
        source_id: str(
            live_cli_run.host_report.parent / "phase-0a" / "fixture" / filename
        )
        for source_id, filename in (
            ("source", "source.mov"),
            ("intro", "intro.mov"),
            ("outro", "outro.mov"),
        )
    }
    expected = expected_from_manifest(manifest, media)
    rate = RationalFrameRate(
        num=manifest.recipe.source.frame_rate.num, den=manifest.recipe.source.frame_rate.den
    )
    assert len(report.items) == 8
    rows = {(row.requested.item_id, row.requested.kind): row for row in report.items}
    for want in expected.items:
        row = rows[(want.item_id, want.kind)]
        assert row.requested == requested_placement(want, rate, FRAME_ORIGIN)
        assert row.observed == row.requested
    assert report.timeline_fingerprint == timeline_fingerprint(
        tuple(row.observed for row in report.items)
    )
    render = Path(report.render_output.output_path)
    assert render.is_file()
    assert hashlib.sha256(render.read_bytes()).hexdigest() == report.output_hash
    assert render.stat().st_size == report.render_output.byte_size
    decode = subprocess.run(
        [
            str(live_cli_run.ffmpeg),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(render),
            "-f",
            "null",
            "-",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert decode.returncode == 0, decode.stderr[-2000:]
    assert report.render_job.completion_percentage == 100
    assert report.render_job.completion_source == "CompletionPercentage"
    assert report.render_job.poll_count >= 1
    assert report.render_output.decode.exit_code == 0
    assert report.failures == ()
    assert report.bindings.host_report_sha256 == hashlib.sha256(
        live_cli_run.host_report.read_bytes()
    ).hexdigest()
    assert report.bindings.manifest_sha256 == hashlib.sha256(MANIFEST.read_bytes()).hexdigest()


@pytest.mark.resolve_live
def test_live_build_report_verify_cli(live_cli_run: LiveRun) -> None:
    if live_cli_run.report_path is None:
        pytest.skip("no report artifact written; run with --resolve-evidence")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.resolve_bridge.build_report",
            "--manifest",
            str(MANIFEST),
            "--fixture-dir",
            str(live_cli_run.host_report.parent / "phase-0a" / "fixture"),
            "--host-report",
            str(live_cli_run.host_report),
            "--verify",
            str(live_cli_run.report_path),
            "--ffmpeg",
            str(live_cli_run.ffmpeg),
            "--ffprobe",
            str(live_cli_run.ffprobe),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "build-report: PASS verify=" in result.stdout


@pytest.mark.resolve_live
def test_live_build_report_evidence_bundle(live_cli_run: LiveRun) -> None:
    bundle = live_cli_run.bundle
    if bundle is None:
        pytest.skip("no --resolve-evidence option; evidence bundle not requested")
    for name in ("ledger.jsonl", "head.json", "build-report.json"):
        assert (bundle / name).is_file(), name
    rows = [json.loads(line) for line in (bundle / "ledger.jsonl").read_text().splitlines()]
    assert rows[-1]["event_type"] == "completion"
    assert rows[-1]["exit_code"] == 0


@pytest.mark.resolve_live
def test_live_build_report_leaves_no_owned_projects(
    live_connection: ResolveConnection,
) -> None:
    manager = live_connection.project_manager()
    owned = [
        name for name in manager.GetProjectListInCurrentFolder() if name.startswith(PROJECT_PREFIX)
    ]
    assert owned == [], f"build-report CLI leaked owned projects: {owned}"
