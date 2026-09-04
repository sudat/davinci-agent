"""Typed filesystem operations for the insertion run's output, work, and evidence.

Every expected OSError at these seams becomes a typed ChapterCardInsertError so
the CLI can report a clean refusal instead of a traceback.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from services.cli._v44_chapter_card_gates import ChapterCardInsertError

TEMP_PREFIX = ".v44-chapter-card-"


def require_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ChapterCardInsertError(
            "fs-dir-failed", f"cannot create directory {path}: {error}"
        ) from error


def write_file(path: Path, data: bytes) -> None:
    try:
        path.write_bytes(data)
    except OSError as error:
        raise ChapterCardInsertError(
            "fs-write-failed", f"cannot write {path}: {error}"
        ) from error


def write_synced(path: Path, data: bytes) -> None:
    """Write and fsync an already-exclusive file; typed refusal on failure."""

    try:
        with path.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise ChapterCardInsertError(
            "fs-write-failed", f"cannot write {path}: {error}"
        ) from error


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as error:
        raise ChapterCardInsertError(
            "fs-stat-failed", f"cannot stat {path}: {error}"
        ) from error


def exclusive_temp(directory: Path, suffix: str) -> Path:
    """Reserve an exclusively-created file (no symlink, no guessing) in `directory`."""

    try:
        descriptor, name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=suffix, dir=directory)
    except OSError as error:
        raise ChapterCardInsertError(
            "fs-temp-failed", f"cannot reserve a temporary file in {directory}: {error}"
        ) from error
    os.close(descriptor)
    return Path(name)


def exclusive_dir(parent: Path, prefix: str) -> Path:
    """Reserve an exclusively-created staging directory under `parent`."""

    try:
        return Path(tempfile.mkdtemp(prefix=TEMP_PREFIX + prefix, dir=parent))
    except OSError as error:
        raise ChapterCardInsertError(
            "fs-temp-failed", f"cannot reserve a staging directory in {parent}: {error}"
        ) from error


def replace_file(source: Path, destination: Path) -> None:
    try:
        source.replace(destination)
    except OSError as error:
        raise ChapterCardInsertError(
            "fs-replace-failed", f"cannot publish {destination}: {error}"
        ) from error


__all__ = [
    "exclusive_dir",
    "exclusive_temp",
    "file_size",
    "replace_file",
    "require_dir",
    "write_file",
    "write_synced",
]
