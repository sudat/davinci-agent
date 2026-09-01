"""Publishability record binding (reader side).

``gate-summary``/current readers must fail closed on publishability records
that are unbound (no viewed_render_sha256 — e.g. the stale 2026-08-29
verdict), bind a render other than the current render truth, or belong to a
foreign finishing run. Writer-side binding lives in
``test_v44_record_publishability.py``. Environment helpers come from the
finishing harness module.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.mcp_execution.native_render_meta import NativeRenderMeta
from services.metrics.v44_gate_state import V44GateSummaryV1
from services.preview.errors import PreviewError
from services.preview.tools import PinnedTools, load_pinned_tools
from services.qc.policy_build import build_policy
from tests.cli.test_v44_finishing import (
    EPISODE_ID,
    FULL_SELECTIONS,
    _kit_record,
    _run_cli,
    _workspace,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(scope="module")
def tools() -> Iterator[PinnedTools]:
    try:
        yield load_pinned_tools()
    except (PreviewError, OSError) as error:
        pytest.skip(f"pinned preview toolchain unavailable: {error}")


@pytest.fixture(scope="module")
def fixture_clip(tools: PinnedTools, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Real mp4: lavfi testsrc2 + sine 48 kHz, h264_videotoolbox/aac."""

    media = tmp_path_factory.mktemp("v44-pub-reader-fixture") / "clip.mp4"
    result = subprocess.run(
        (
            str(tools.ffmpeg), "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v", "h264_videotoolbox", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "1", str(media),
        ),
        check=False, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]
    return media


def _seed_episode(tmp_path: Path, fixture_clip: Path) -> Path:
    episode_root = _workspace(tmp_path, fixture_clip, _kit_record(*FULL_SELECTIONS))
    result = _run_cli(["run", "--episode-root", str(episode_root), "--executor", "fake"])
    assert result.returncode == 1  # honest blocked without a QC policy
    return episode_root


def _bind_render_meta(episode_root: Path, sha256: str) -> None:
    meta = NativeRenderMeta(
        schema_version="native-render-meta-v1",
        custom_name=f"finishing-native-{EPISODE_ID}",
        job_id="job-fixture-0001",
        output_path=str(episode_root / "finishing" / "final-preview" / "preview.mp4"),
        output_sha256=sha256,
        duration_seconds=3.0,
        video_codec="h264",
        width=320,
        height=180,
        avg_frame_rate="30/1",
        audio_codec="aac",
        audio_channels=1,
        audio_sample_rate=48000,
        has_subtitle_stream=False,
    )
    meta_dir = episode_root / "finishing" / "final-resolve-render"
    meta_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(
        meta_dir / f"finishing-native-{EPISODE_ID}.meta.json",
        canonical_model_bytes(meta),
    )


def _current_render_sha(episode_root: Path) -> str:
    return sha256_file(episode_root / "finishing" / "final-preview" / "preview.mp4")


def _rerun_with_policy(episode_root: Path) -> None:
    policy_path = episode_root / "finishing" / "qc-policy.json"
    atomic_write(
        policy_path,
        canonical_model_bytes(
            build_policy(episode_root / "finishing" / "final-preview" / "preview.mp4")
        ),
    )
    result = _run_cli(
        ["run", "--episode-root", str(episode_root), "--executor", "fake",
         "--qc-policy", str(policy_path)]
    )
    assert result.returncode == 0, result.stderr


def _write_review(episode_root: Path, payload: dict[str, str]) -> None:
    target = episode_root / "review" / "publishability.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())


def _run_id(episode_root: Path) -> str:
    report = json.loads((episode_root / "finishing" / "finishing-run.json").read_bytes())
    return str(report["run_id"])


def test_gate_summary_refuses_without_publishability_record(
    episode_root_ready: Path,
) -> None:
    """The plan's failure drill: no publishability record -> typed refusal."""

    episode_root = episode_root_ready
    assert not (episode_root / "review" / "publishability.json").exists()
    result = _run_cli(["gate-summary", "--episode-root", str(episode_root)])
    assert result.returncode == 1
    assert "publishability-missing" in result.stderr
    assert "record-publishability" in result.stderr
    assert not (episode_root / "finishing" / "gate-summary.json").is_file()


def test_gate_summary_refuses_unbound_record(episode_root_ready: Path) -> None:
    episode_root = episode_root_ready
    _write_review(
        episode_root,
        {
            "schema_version": "publishability-review-v1",
            "episode_id": EPISODE_ID,
            "run_id": _run_id(episode_root),
            "publishable": "as_is",
        },
    )
    result = _run_cli(["gate-summary", "--episode-root", str(episode_root)])
    assert result.returncode == 1
    assert "publishability-unbound" in result.stderr
    assert not (episode_root / "finishing" / "gate-summary.json").is_file()


def test_gate_summary_refuses_render_mismatch(episode_root_ready: Path) -> None:
    episode_root = episode_root_ready
    _write_review(
        episode_root,
        {
            "schema_version": "publishability-review-v1",
            "episode_id": EPISODE_ID,
            "run_id": _run_id(episode_root),
            "publishable": "as_is",
            "viewed_render_sha256": "c" * 64,
            "viewed_at": "2026-09-02T00:00:00Z",
        },
    )
    result = _run_cli(["gate-summary", "--episode-root", str(episode_root)])
    assert result.returncode == 1
    assert "publishability-render-mismatch" in result.stderr
    assert _current_render_sha(episode_root) in result.stderr
    assert not (episode_root / "finishing" / "gate-summary.json").is_file()


def test_gate_summary_refuses_foreign_run(episode_root_ready: Path) -> None:
    episode_root = episode_root_ready
    _write_review(
        episode_root,
        {
            "schema_version": "publishability-review-v1",
            "episode_id": EPISODE_ID,
            "run_id": "ep-foreign-finishing",
            "publishable": "as_is",
            "viewed_render_sha256": _current_render_sha(episode_root),
            "viewed_at": "2026-09-02T00:00:00Z",
        },
    )
    result = _run_cli(["gate-summary", "--episode-root", str(episode_root)])
    assert result.returncode == 1
    assert "publishability-run-mismatch" in result.stderr
    assert not (episode_root / "finishing" / "gate-summary.json").is_file()


def test_gate_summary_honest_fail_then_pass_after_full_evidence(
    episode_root_ready: Path,
) -> None:
    """Binding first, then the honest arc: a summary without the full AHT log
    writes passed=false (exit 1); after all five phases are recorded the same
    evidence derives passed=true."""

    episode_root = episode_root_ready
    current = _current_render_sha(episode_root)
    verdict = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable", "--viewed-render-sha256", current]
    )
    assert verdict.returncode == 0, verdict.stderr

    early = _run_cli(["gate-summary", "--episode-root", str(episode_root)])
    assert early.returncode == 1  # no time log yet
    summary_path = episode_root / "finishing" / "gate-summary.json"
    early_summary = json.loads(summary_path.read_bytes())
    assert early_summary["passed"] is False
    assert early_summary["bootstrap_aht_minutes"] is None
    assert early_summary["direct_resolve_minutes"] is None
    assert "not passed: bootstrap AHT minutes not recorded" in early.stderr

    for phase, minutes in (
        ("ordinary_review", "12.5"),
        ("kit_bootstrap", "3.5"),
        ("taste_calibration", "1.5"),
        ("troubleshooting", "2.5"),
        ("direct_resolve", "40"),
    ):
        timed = _run_cli(
            ["record-time", "--episode-root", str(episode_root),
             "--phase", phase, "--minutes", minutes]
        )
        assert timed.returncode == 0, timed.stderr

    final = _run_cli(
        ["gate-summary", "--episode-root", str(episode_root),
         "--subtitle-proof-ref", "finishing/subtitle-proof/subtitle-proof.json"]
    )
    assert final.returncode == 0, final.stderr
    summary = V44GateSummaryV1.model_validate_json(summary_path.read_bytes())
    assert summary.passed is True
    assert summary.operator_verdict == "publishable"
    assert summary.blocked_domains == ()
    assert summary.technical_qc == "passed"
    assert summary.bootstrap_aht_minutes == 60.0
    assert summary.direct_resolve_minutes == 40.0
    assert "review/publishability.json" in summary.artifacts


def test_gate_summary_not_publishable_verdict_never_passes(
    episode_root_ready: Path,
) -> None:
    episode_root = episode_root_ready
    current = _current_render_sha(episode_root)
    verdict = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "not_publishable", "--viewed-render-sha256", current]
    )
    assert verdict.returncode == 0, verdict.stderr
    for phase, minutes in (("ordinary_review", "5"), ("direct_resolve", "1.5")):
        _run_cli(
            ["record-time", "--episode-root", str(episode_root),
             "--phase", phase, "--minutes", minutes]
        )

    result = _run_cli(["gate-summary", "--episode-root", str(episode_root)])
    assert result.returncode == 1
    assert "operator verdict is not_publishable" in result.stderr
    summary = V44GateSummaryV1.model_validate_json(
        (episode_root / "finishing" / "gate-summary.json").read_bytes()
    )
    assert summary.passed is False
    assert summary.operator_verdict == "not_publishable"


@pytest.fixture
def episode_root_ready(episode_root_seeded: Path) -> Path:
    """A seeded episode whose render truth is bound: preview meta written and
    the QC rerun agreeing with it."""

    episode_root = episode_root_seeded
    _rerun_with_policy(episode_root)
    _bind_render_meta(episode_root, _current_render_sha(episode_root))
    return episode_root


@pytest.fixture
def episode_root_seeded(tmp_path: Path, fixture_clip: Path) -> Path:
    return _seed_episode(tmp_path, fixture_clip)
