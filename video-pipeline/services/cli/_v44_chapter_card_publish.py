"""Publication transaction for the master/evidence/QA artifact set.

Guarantees exception-level rollback: any OSError during backup, placement, or
sync restores the entire prior artifact set, and backups survive a failed
restoration. This is deliberately NOT crash-level multi-file atomicity - a
process kill between the backup moves and the publish renames leaves the
backups on disk for manual recovery. All renames stay inside the run-owned
output directory (same filesystem), and every staged or backup name is
exclusively reserved, so no preexisting symlink is ever followed.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from services.cli._v44_chapter_card_gates import ChapterCardInsertError

BACKUP_PREFIX: Final = ".v44-backup-"


@dataclass(frozen=True, slots=True)
class Publication:
    """The three published artifacts and their staged replacements."""

    output_dir: Path
    master: Path
    evidence: Path
    qa_dir: Path
    staged_master: Path
    staged_evidence: Path
    staged_qa: Path


@dataclass(slots=True)
class _Artifact:
    final: Path
    staged: Path
    is_dir: bool
    backup: Path | None = None
    backed_up: bool = False
    placed: bool = field(default=False)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_staged(publication: Publication) -> None:
    staged_files = [publication.staged_master, publication.staged_evidence]
    staged_files.extend(sorted(p for p in publication.staged_qa.rglob("*") if p.is_file()))
    try:
        for path in staged_files:
            _fsync_file(path)
        _fsync_dir(publication.staged_qa)
    except OSError as error:
        raise ChapterCardInsertError(
            "fs-sync-failed", f"cannot flush staged artifacts: {error}"
        ) from error


def _reserve_file_name(directory: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=BACKUP_PREFIX, dir=directory)
    os.close(descriptor)
    return Path(name)


def _reserve_dir_name(directory: Path) -> Path:
    reserved = Path(tempfile.mkdtemp(prefix=BACKUP_PREFIX, dir=directory))
    reserved.rmdir()
    return reserved


def _back_up(artifact: _Artifact) -> None:
    """Move the current final to a reserved backup; completion is explicit.

    The reservation only counts as a backup once the rename succeeds - an
    empty reserved placeholder must never be restored over a real artifact,
    so a failed rename deletes its own reservation and re-raises.
    """

    if not os.path.lexists(artifact.final):
        return
    reserved = (
        _reserve_dir_name(artifact.final.parent) if artifact.is_dir
        else _reserve_file_name(artifact.final.parent)
    )
    try:
        artifact.final.replace(reserved)
    except OSError:
        with suppress(OSError):
            _remove(reserved, is_dir=artifact.is_dir)
        raise
    artifact.backup = reserved
    artifact.backed_up = True


def _place(artifact: _Artifact) -> None:
    artifact.staged.replace(artifact.final)
    artifact.placed = True


def _remove(path: Path, *, is_dir: bool) -> None:
    if is_dir:
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _restore(plan: list[_Artifact]) -> None:
    failures: list[str] = []
    for artifact in reversed(plan):
        if artifact.placed:
            with suppress(OSError):
                _remove(artifact.final, is_dir=artifact.is_dir)
        if artifact.backed_up and artifact.backup is not None:
            try:
                artifact.backup.replace(artifact.final)
            except OSError as error:
                failures.append(f"{artifact.backup.name}: {error}")
    with suppress(OSError):
        _fsync_dir(plan[0].final.parent)
    if failures:
        preserved = sorted(
            str(artifact.backup)
            for artifact in plan
            if artifact.backed_up and artifact.backup is not None
        )
        raise ChapterCardInsertError(
            "publish-rollback-failed",
            f"restoration failed ({'; '.join(failures)}); backups preserved: {preserved}",
        )


def publish(publication: Publication) -> None:
    """Back up the current set, place the staged set, roll back on any failure."""

    _sync_staged(publication)
    plan = [
        _Artifact(publication.master, publication.staged_master, is_dir=False),
        _Artifact(publication.evidence, publication.staged_evidence, is_dir=False),
        _Artifact(publication.qa_dir, publication.staged_qa, is_dir=True),
    ]
    try:
        for artifact in plan:
            _back_up(artifact)
        for artifact in plan:
            _place(artifact)
        with suppress(OSError):
            _fsync_dir(publication.output_dir)
    except OSError as error:
        _restore(plan)
        raise ChapterCardInsertError(
            "fs-replace-failed", f"cannot publish the artifact set: {error}"
        ) from error
    for artifact in plan:
        if artifact.backed_up and artifact.backup is not None:
            with suppress(OSError):
                _remove(artifact.backup, is_dir=artifact.is_dir)


__all__ = ["Publication", "publish"]
