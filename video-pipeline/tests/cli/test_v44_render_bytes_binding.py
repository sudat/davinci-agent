"""Render byte-truth binding (the exact-viewed-bytes guarantee).

``resolve_current_render_sha256`` must hash the actual render bytes named by
``NativeRenderMeta.output_path`` — metadata/QC agreement alone cannot detect a
same-path byte replacement. Fail closed on missing, outside-root, symlink /
non-regular / unreadable outputs and on any hash divergence from the bound
meta/QC sha. Environment helpers come from the writer test module.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.cli.test_v44_record_publishability import (
    _bind_render_meta,
    _current_render_sha,
    _rerun_with_policy,
    _run_cli,
    _seed_episode,
    fixture_clip,
    tools,
)

__all__ = ["fixture_clip", "tools"]


def _prepared(tmp_path: Path, fixture_clip: Path) -> tuple[Path, str]:
    episode_root = _seed_episode(tmp_path, fixture_clip)
    _rerun_with_policy(episode_root)
    current = _current_render_sha(episode_root)
    _bind_render_meta(episode_root, current)
    return episode_root, current


def _record(
    episode_root: Path, viewed: str
) -> subprocess.CompletedProcess[str]:
    return _run_cli(
        ["record-publishability", "--episode-root", str(episode_root),
         "--verdict", "publishable", "--viewed-render-sha256", viewed]
    )


def _assert_refused(episode_root: Path, result, code: str) -> None:
    assert result.returncode == 1, result.stderr
    assert code in result.stderr
    assert not (episode_root / "review" / "publishability.json").exists()


def test_record_publishability_refuses_missing_render_bytes(
    tmp_path: Path, fixture_clip: Path
) -> None:
    episode_root, current = _prepared(tmp_path, fixture_clip)
    (episode_root / "finishing" / "final-preview" / "preview.mp4").unlink()

    _assert_refused(
        episode_root, _record(episode_root, current), "render-output-missing"
    )


def test_record_publishability_refuses_replaced_render_bytes(
    tmp_path: Path, fixture_clip: Path
) -> None:
    episode_root, current = _prepared(tmp_path, fixture_clip)
    preview = episode_root / "finishing" / "final-preview" / "preview.mp4"
    preview.write_bytes(b"tampered-render-bytes")

    _assert_refused(
        episode_root, _record(episode_root, current), "render-bytes-mismatch"
    )


def test_record_publishability_refuses_render_outside_episode_root(
    tmp_path: Path, fixture_clip: Path
) -> None:
    episode_root, current = _prepared(tmp_path, fixture_clip)
    outside = tmp_path / "outside-render.mp4"
    outside.write_bytes(b"outside bytes")
    meta_path = (
        episode_root / "finishing" / "final-resolve-render"
        / "finishing-native-ep-v44-finishing.meta.json"
    )
    meta_path.write_text(
        meta_path.read_text().replace(
            str(episode_root / "finishing" / "final-preview" / "preview.mp4"),
            str(outside),
        )
    )

    _assert_refused(
        episode_root, _record(episode_root, current),
        "render-output-outside-episode-root",
    )


def test_record_publishability_refuses_symlinked_render_bytes(
    tmp_path: Path, fixture_clip: Path
) -> None:
    episode_root, current = _prepared(tmp_path, fixture_clip)
    preview = episode_root / "finishing" / "final-preview" / "preview.mp4"
    target = episode_root / "finishing" / "elsewhere.mp4"
    target.write_bytes(b"linked bytes")
    preview.unlink()
    preview.symlink_to(target)

    _assert_refused(
        episode_root, _record(episode_root, current), "render-output-unreadable"
    )


def test_gate_summary_refuses_render_bytes_replaced_after_recording(
    tmp_path: Path, fixture_clip: Path
) -> None:
    episode_root, current = _prepared(tmp_path, fixture_clip)
    recorded = _record(episode_root, current)
    assert recorded.returncode == 0, recorded.stderr
    preview = episode_root / "finishing" / "final-preview" / "preview.mp4"
    preview.write_bytes(b"replaced after the operator looked")

    result = _run_cli(["gate-summary", "--episode-root", str(episode_root)])
    assert result.returncode == 1
    assert "render-bytes-mismatch" in result.stderr
    assert not (episode_root / "finishing" / "gate-summary.json").is_file()
