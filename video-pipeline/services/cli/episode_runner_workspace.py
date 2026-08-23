"""Workspace adaptation: cockpit episode dir -> the chain's episode_root (task 7).

``run_real_chain`` expects an episode root carrying ``episode.json`` whose
``video_path`` resolves to the camera original, and it writes everything
else into a separate out dir; the cockpit episode directory instead holds
``brief.json``/``intake.json`` and serves its preview from ``previews/``.
This module bridges the two layouts WITHOUT copying media: the chain
manifest points at the absolute original, chain outputs land under
``<episode-root>/run/``, and the rendered preview is hard-linked (copied
only when linking is impossible) to where the cockpit serves it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.cli.episode_runner_state import log_event
from services.cli.real_episode import EPISODE_MANIFEST_NAME, RealEpisodeManifest
from services.foundation_io import atomic_write, canonical_model_bytes
from services.preview.render import PREVIEW_NAME

if TYPE_CHECKING:
    from typing import BinaryIO

RUN_DIR_NAME: Final = "run"
PREVIEW_DIR_NAME: Final = "preview-v1"
VIDEO_EXTENSIONS: Final = frozenset({".mov", ".mp4", ".m4v", ".mts", ".m2ts"})


class WorkspaceAdaptationError(Exception):
    """Typed blocked-refusal when the intake source cannot feed the chain."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def principal_video(source_folder: Path) -> Path:
    """The one camera file the H1 talking-head contract runs on (largest)."""

    if not source_folder.is_dir():
        raise WorkspaceAdaptationError(
            "source-folder-missing", f"intake source folder is gone: {source_folder}"
        )
    candidates = [
        path
        for path in source_folder.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not candidates:
        raise WorkspaceAdaptationError(
            "source-video-missing",
            f"no camera video ({sorted(VIDEO_EXTENSIONS)}) under {source_folder}",
        )
    return max(candidates, key=lambda path: path.stat().st_size)


def write_chain_manifest(episode_root: Path, episode_id: str, video: Path) -> None:
    atomic_write(
        episode_root / EPISODE_MANIFEST_NAME,
        canonical_model_bytes(
            RealEpisodeManifest(
                schema_version="real-episode-v1",
                episode_id=episode_id,
                video_path=str(video.resolve()),
                audio_path=None,
                language="ja",
                declared_privacy_flags=(),
                declared_rights_flags=(),
                fixture_only=False,
            )
        ),
    )


def publish_preview(episode_root: Path, log: BinaryIO) -> None:
    """Link run/preview-v1/preview.mp4 to previews/preview.mp4 (cockpit path)."""

    source = episode_root / RUN_DIR_NAME / PREVIEW_DIR_NAME / PREVIEW_NAME
    target = episode_root / "previews" / PREVIEW_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)
    log_event(log, "preview_published", path=str(target))


__all__ = [
    "PREVIEW_DIR_NAME",
    "RUN_DIR_NAME",
    "VIDEO_EXTENSIONS",
    "WorkspaceAdaptationError",
    "principal_video",
    "publish_preview",
    "write_chain_manifest",
]
