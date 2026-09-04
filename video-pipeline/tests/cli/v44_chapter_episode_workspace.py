"""Protected-episode workspace and pinned-tool fixtures for chapter-card tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.foundation_io import sha256_file
from services.preview.models import PreviewToolchainError
from services.preview.tools import PinnedTools, load_pinned_tools
from tests.cli.v44_chapter_sidecar_factory import EPISODE_ID


def episode_workspace(root: Path) -> tuple[Path, Path]:
    """A workspace holding all 18 protected files with stable bytes."""

    episode_root = root / "episodes" / EPISODE_ID
    diag_root = root / "diag" / "finishing"
    files = (
        episode_root / "review/store/plan-v1.json",
        episode_root / "review/store/plan-v2.json",
        episode_root / "review/store/plan-v3.json",
        episode_root / "review/store/ir-v1.json",
        episode_root / "review/store/ir-v2.json",
        episode_root / "review/store/ir-v3.json",
        episode_root / "review/store/versions.json",
        episode_root / "review/events.jsonl",
        episode_root / "review/events.jsonl.seal",
        episode_root / "review-store/events.jsonl",
        episode_root / "review-store/events.jsonl.seal",
        episode_root / "review-store/plan-v1.json",
        episode_root / "review-store/ir-v1.json",
        episode_root / "review-store/versions.json",
        episode_root / "runtime/chapter-title-proposal.json",
        diag_root / "theme-full-render/v44-real-01-theme-full.mp4",
        diag_root / f"final-resolve-render/finishing-native-{EPISODE_ID}.mp4",
        diag_root / f"resolve-render/{EPISODE_ID}-final-v5.mp4",
    )
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"protected:" + path.name.encode())
    return episode_root, diag_root


def fake_tools(ffmpeg: Path, ffprobe: Path) -> PinnedTools:
    """Pins that verify against whatever bytes the paths currently hold."""

    return PinnedTools(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        ffmpeg_sha256=sha256_file(ffmpeg),
        ffprobe_sha256=sha256_file(ffprobe),
    )


def pinned_tools_or_skip() -> PinnedTools:
    try:
        return load_pinned_tools()
    except PreviewToolchainError:
        pytest.skip("pinned ffmpeg toolchain unavailable")


__all__ = ["episode_workspace", "fake_tools", "pinned_tools_or_skip"]
