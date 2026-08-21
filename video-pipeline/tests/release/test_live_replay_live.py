"""The ONE live test for the live fault-injecting replay (Todo 67).

``resolve_live``-marked, self-cleaning, and honest: it drives the REAL
flow (live bridge, pinned toolchain, real A-profile build and render)
with three injection routes exercised live (partial-build with a real
mid-build abort and clean rebuild, stale-state typed rejection, and
false-success detection by recompute against the real render). Resolve
restart and the full A/B swap stay in the F3 lane run; their offline
fault shapes are covered in ``test_live_replay.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.release.extract_candidate import main as extract_main
from services.release.live_flow import run_live_replay
from services.release.live_wiring import pinned_media_bins, production_seams
from services.resolve_bridge.connection import connect
from services.resolve_bridge.lifecycle import owned_project_names
from services.resolve_bridge.readiness import load_host_report
from tests.release.support import build_test_candidate


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


@pytest.mark.resolve_live
def test_live_replay_partial_stale_false_success(tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    report = _report_path(request.config)
    if report is None:
        pytest.skip(
            "resolve host report not found: pass --resolve-evidence or set RESOLVE_HOST_REPORT"
        )
    try:
        ffmpeg, ffprobe = pinned_media_bins()
    except Exception as error:  # noqa: BLE001 (skip on any toolchain failure)
        pytest.skip(f"pinned phase-3 toolchain unavailable: {error}")
    _repo, candidate, _sha = build_test_candidate(tmp_path)
    extract = tmp_path / "extract"
    assert extract_main(["--candidate", str(candidate), "--no-follow", "--out", str(extract)]) == 0
    out = tmp_path / "out"
    seams = production_seams(
        host_report_path=report, ffmpeg=ffmpeg, ffprobe=ffprobe, out_root=out
    )
    summary = run_live_replay(
        extract=extract,
        h1_binding=extract / "inputs" / "h1" / "binding.json",
        injections=("partial-build", "stale-state", "false-success"),
        profiles=("a",),
        out=out,
        seams=seams,
    )
    assert summary.verdict == "diagnostic-passed", summary.failure_codes
    assert summary.verdict_scope == "diagnostic"
    assert summary.lease.acquired
    assert summary.lease.released
    outcomes = {row.route: row.outcome for row in summary.injections}
    assert outcomes["partial-build"] == "recovered_clean_rebuild"
    assert outcomes["stale-state"] == "typed_rejection_no_silent_use"
    assert outcomes["false-success"] == "tamper_detected_by_recompute"
    final = out / "render" / "final.mp4"
    assert final.is_file()
    assert summary.final_render.policy_verified
    policy = summary.final_render.policy_measured
    assert (policy.container, policy.video_codec, policy.audio_codec) == ("mp4", "h264", "aac")
    assert (policy.width, policy.height, policy.frame_rate) == (1920, 1080, "30/1")
    assert policy.frame_count == 600
    assert policy.audio_sample_rate_hz == 48000
    anchors = {row.position: row.frame_sha256 for row in summary.final_render.anchors}
    assert len(anchors) == 5
    from services.release.live_models import LiveReplaySummary  # noqa: PLC0415

    assert LiveReplaySummary.model_validate_json(
        (out / "run-summary.json").read_bytes()
    ) == summary
    manager = connect(load_host_report(report)).project_manager()
    assert owned_project_names(manager) == ()
