"""Protection set for the chapter-card insertion run (TDD)."""


from __future__ import annotations

from pathlib import Path

from services.cli.v44_chapter_card_insert import hash_protected, protected_paths
from tests.cli.v44_chapter_episode_workspace import episode_workspace


def _episode_workspace(tmp_path: Path) -> tuple[Path, Path]:
    return episode_workspace(tmp_path)


def test_protected_paths_cover_store_events_renders_and_sidecar(tmp_path: Path) -> None:
    # Given: a workspace with every protected file present
    # When: enumerating the protection set
    # Then: all 18 files are listed and every one exists
    episode_root, diag_root = _episode_workspace(tmp_path)
    paths = protected_paths(episode_root=episode_root, diag_finishing_root=diag_root)
    assert len(paths) == 18
    assert all(path.is_file() for path in paths)
    assert any("plan-v3.json" in str(path) for path in paths)
    assert any("chapter-title-proposal.json" in str(path) for path in paths)
    assert any("v44-real-01-theme-full.mp4" in str(path) for path in paths)


def test_hash_protected_detects_drift(tmp_path: Path) -> None:
    # Given: a hashed protection set where one file then changes
    # When: the set is hashed again
    # Then: the drifted file's hash changes and is identified by relative path
    episode_root, diag_root = _episode_workspace(tmp_path)
    paths = protected_paths(episode_root=episode_root, diag_finishing_root=diag_root)
    before = hash_protected(paths, root=tmp_path)
    target = next(path for path in paths if path.name == "plan-v3.json")
    target.write_bytes(b"tampered")
    after = hash_protected(paths, root=tmp_path)
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {"episodes/ep-457dfac97989568e/review/store/plan-v3.json"}
