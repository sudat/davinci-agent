"""Publishability record binding (writer side).

``record-publishability`` must bind the verdict to the exact operator-viewed
current render sha256 (resolved from one unambiguous ``native-render-meta-v1``
record agreeing with the ``qc-report-v1`` render input) and refuse — writing
nothing — on missing/ambiguous/disagreeing render truth. Reader-side refusal
lives in ``test_v44_gate_summary_binding.py``. Environment helpers come from
the finishing harness module.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.final_review.publishability import PublishabilityReviewV1
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.mcp_execution.native_render_meta import NativeRenderMeta
from services.preview.errors import PreviewError
from services.preview.tools import PinnedTools, load_pinned_tools
from services.qc.models import QcInputBinding, QcReport, QcToolVersions
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

STALE_RENDER_SHA = "2c4974d3" + "0" * 56


@pytest.fixture(scope="module")
def tools() -> Iterator[PinnedTools]:
    try:
        yield load_pinned_tools()
    except (PreviewError, OSError) as error:
        pytest.skip(f"pinned preview toolchain unavailable: {error}")


@pytest.fixture(scope="module")
def fixture_clip(tools: PinnedTools, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Real mp4: lavfi testsrc2 + sine 48 kHz, h264_videotoolbox/aac."""

    media = tmp_path_factory.mktemp("v44-pub-binding-fixture") / "clip.mp4"
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


def test_record_publishability_requires_viewed_render_sha(
    tmp_path: Path, fixture_clip: Path
) -> None:
    episode_root = _seed_episode(tmp_path, fixture_clip)
    result = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable"]
    )
    assert result.returncode == 2
    assert "--viewed-render-sha256" in result.stderr
    assert not (episode_root / "review" / "publishability.json").exists()


def test_record_publishability_refuses_unfinished_state(tmp_path: Path) -> None:
    episode_root = tmp_path / "ep-unfinished"
    episode_root.mkdir()
    result = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable", "--viewed-render-sha256", "a" * 64]
    )
    assert result.returncode == 1
    assert "finishing-report-missing" in result.stderr
    assert not (episode_root / "review" / "publishability.json").exists()


def test_record_publishability_round_trip(tmp_path: Path, fixture_clip: Path) -> None:
    episode_root = _seed_episode(tmp_path, fixture_clip)
    _rerun_with_policy(episode_root)
    current = _current_render_sha(episode_root)
    _bind_render_meta(episode_root, current)

    result = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable_after_fixes",
         "--comments", "全体として良好",
         "--dimension-comments", "audio_finishing=BGMをもう少し下げたい",
         "--dimension-comments", "subtitle=固有名詞は正確",
         "--viewed-render-sha256", current]
    )
    assert result.returncode == 0, result.stderr
    review = PublishabilityReviewV1.model_validate_json(
        (episode_root / "review" / "publishability.json").read_bytes()
    )
    assert review.publishable == "after_small_corrections"
    assert review.episode_id == EPISODE_ID
    assert review.run_id.endswith("-finishing")
    assert review.overall_comment is not None
    assert "全体として良好" in review.overall_comment
    assert "audio_finishing: BGMをもう少し下げたい" in review.overall_comment
    assert "subtitle: 固有名詞は正確" in review.overall_comment
    assert review.viewed_render_sha256 == current
    assert review.viewed_at is not None
    assert review.viewed_at.endswith("Z")


def test_record_publishability_rejects_unknown_dimension(
    tmp_path: Path, fixture_clip: Path
) -> None:
    episode_root = _seed_episode(tmp_path, fixture_clip)
    current = _current_render_sha(episode_root)
    _bind_render_meta(episode_root, current)
    result = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable",
         "--dimension-comments", "not_a_domain=text",
         "--viewed-render-sha256", current]
    )
    assert result.returncode == 2
    assert "dimension-unknown" in result.stderr


def test_record_publishability_refuses_stale_viewed_render_sha(
    tmp_path: Path, fixture_clip: Path
) -> None:
    episode_root = _seed_episode(tmp_path, fixture_clip)
    _rerun_with_policy(episode_root)
    current = _current_render_sha(episode_root)
    _bind_render_meta(episode_root, current)
    result = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable",
         "--viewed-render-sha256", STALE_RENDER_SHA]
    )
    assert result.returncode == 1
    assert "viewed-render-sha-mismatch" in result.stderr
    assert current in result.stderr
    assert not (episode_root / "review" / "publishability.json").exists()


def test_record_publishability_refuses_meta_qc_disagreement(
    tmp_path: Path, fixture_clip: Path
) -> None:
    episode_root = _seed_episode(tmp_path, fixture_clip)
    _bind_render_meta(episode_root, "a" * 64)
    atomic_write(
        episode_root / "finishing" / "qc-report.json",
        canonical_model_bytes(
            QcReport(
                schema_version="qc-report-v1",
                verdict="passed",
                tool_versions=QcToolVersions(
                    qc_engine="fixture", ffmpeg_sha256="0" * 64, ffprobe_sha256="0" * 64
                ),
                threshold_version="fixture-v1",
                inputs=(QcInputBinding(kind="render", sha256="b" * 64),),
            )
        ),
    )
    result = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable", "--viewed-render-sha256", "a" * 64]
    )
    assert result.returncode == 1
    assert "render-truth-disagreement" in result.stderr
    assert not (episode_root / "review" / "publishability.json").exists()


def test_record_publishability_rejects_malformed_sha(
    tmp_path: Path, fixture_clip: Path
) -> None:
    episode_root = _seed_episode(tmp_path, fixture_clip)
    result = _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable", "--viewed-render-sha256", "nothex"]
    )
    assert result.returncode == 2
    assert "viewed-render-sha-invalid" in result.stderr
    assert not (episode_root / "review" / "publishability.json").exists()


def test_old_unbound_payload_still_parses() -> None:
    review = PublishabilityReviewV1.model_validate(
        {
            "schema_version": "publishability-review-v1",
            "episode_id": "ep-old",
            "run_id": "run-old",
            "publishable": "not_yet",
        }
    )
    assert review.viewed_render_sha256 is None
    assert review.viewed_at is None
