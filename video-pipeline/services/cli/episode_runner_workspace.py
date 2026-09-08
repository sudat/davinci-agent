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

from pydantic import ValidationError

from services.cli.bundle import BUNDLE_NAME, BundleDriftError, load_bundle
from services.cli.episode_runner_state import log_event
from services.cli.real_episode import EPISODE_MANIFEST_NAME, RealEpisodeManifest
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file
from services.preview.render import PREVIEW_NAME

if TYPE_CHECKING:
    from typing import BinaryIO

RUN_DIR_NAME: Final = "run"
PREVIEW_DIR_NAME: Final = "preview-v1"
REVIEW_STORE_DIR_NAME: Final = "review-store"
# Cockpit review-store layout (episode_files REVIEW_*_RELATIVE): the sealed
# event log moves OUT of the store dir; plan/IR versions + index live under
# review/store/. The mirror maps the chain's flat run/review-store/ into it.
COCKPIT_REVIEW_LOG_RELATIVE: Final = ("review", "events.jsonl")
COCKPIT_REVIEW_STORE_RELATIVE: Final = ("review", "store")
_LOG_FILE_NAMES: Final = ("events.jsonl", "events.jsonl.seal")
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


def publish_preview(
    episode_root: Path,
    log: BinaryIO,
    source_dir: str = PREVIEW_DIR_NAME,
    *,
    run_id: str | None = None,
) -> None:
    """Link run/<source_dir>/preview.mp4 to previews/preview.mp4 (cockpit path).

    The ``preview_published`` line carries the run-binding evidence:
    ``run_id`` (the caller's RunContext run), ``target_version`` (the
    chain review bundle's ``current`` plan version), ``content_hash``
    (measured from the published file) — each OMITTED when its evidence
    is absent, never guessed.
    """

    source = episode_root / RUN_DIR_NAME / source_dir / PREVIEW_NAME
    target = episode_root / "previews" / PREVIEW_NAME
    if not source.is_file():
        log_event(log, "preview_publish_skipped", reason="source-missing")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)
    log_event(
        log,
        "preview_published",
        path=str(target),
        source=source_dir,
        **_publish_binding(episode_root, target, run_id),
    )


def _publish_binding(
    episode_root: Path, target: Path, run_id: str | None
) -> dict[str, object]:
    """Run-binding evidence taken from the review bundle + the published bytes."""

    evidence: dict[str, object] = {}
    if run_id is not None:
        evidence["run_id"] = run_id
    try:
        bundle = load_bundle(episode_root / RUN_DIR_NAME / BUNDLE_NAME)
    except (BundleDriftError, ValidationError):
        bundle = None
    if bundle is not None:
        evidence["target_version"] = bundle.current.plan_version
    evidence["content_hash"] = sha256_file(target)
    return evidence


def mirror_review_store(episode_root: Path, log: BinaryIO) -> bool:
    """Copy the chain's run/review-store into the cockpit review layout.

    Task-9 review-store adaptation: the cockpit apply path (episode_files
    REVIEW_*_RELATIVE) commits new plan versions under ``review/``, so the
    initial run hands its genesis store over in that layout. COPIES, not
    hard-links: events/seal/versions mutate on later commits and the chain
    copy under ``run/`` stays frozen as the initial-run evidence. Idempotent
    guard: an existing cockpit store (later version already applied) is
    never clobbered.
    """

    source_dir = episode_root / RUN_DIR_NAME / REVIEW_STORE_DIR_NAME
    if not source_dir.is_dir():
        log_event(log, "review_store_mirror_skipped", reason="chain store absent")
        return False
    log_target = episode_root.joinpath(*COCKPIT_REVIEW_LOG_RELATIVE)
    store_target = episode_root.joinpath(*COCKPIT_REVIEW_STORE_RELATIVE)
    if (store_target / "versions.json").is_file():
        log_event(log, "review_store_mirror_skipped", reason="cockpit store exists")
        return False
    log_target.parent.mkdir(parents=True, exist_ok=True)
    store_target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for entry in sorted(source_dir.iterdir()):
        if not entry.is_file():
            continue
        destination = (
            log_target.parent / entry.name
            if entry.name in _LOG_FILE_NAMES
            else store_target / entry.name
        )
        shutil.copyfile(entry, destination)
        copied += 1
    log_event(log, "review_store_mirrored", files=copied)
    return True


__all__ = [
    "COCKPIT_REVIEW_LOG_RELATIVE",
    "COCKPIT_REVIEW_STORE_RELATIVE",
    "PREVIEW_DIR_NAME",
    "REVIEW_STORE_DIR_NAME",
    "RUN_DIR_NAME",
    "VIDEO_EXTENSIONS",
    "WorkspaceAdaptationError",
    "mirror_review_store",
    "principal_video",
    "publish_preview",
    "write_chain_manifest",
]
