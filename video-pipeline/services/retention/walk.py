"""No-follow filesystem reading: job state, registry, and safe traversal.

Symlinks are never followed and never deleted; every directory descent
is containment-checked against the managed root's realpath, and a
visited set makes traversal cycle-proof. All reading uses
``O_NOFOLLOW`` semantics for tamper discipline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from services.artifact_registry.store_view import StoreViewError, read_file_nofollow
from services.retention.errors import RetentionError
from services.retention.models import (
    JOB_STATE_FILE,
    REGISTRY_FILE,
    JobRetentionState,
    RetentionRegistry,
)

WalkKind = Literal["file", "symlink", "dir"]


def _validation_summary(error: ValidationError, *, limit: int = 5) -> str:
    """Field-level failure summary WITHOUT input values (PRD 27 redaction)."""

    parts: list[str] = []
    for item in error.errors()[:limit]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "root"
        parts.append(f"{location}: {item.get('type', 'invalid')}")
    if not parts:
        return "schema validation failed"
    omitted = max(len(error.errors()) - limit, 0)
    suffix = f" (+{omitted} more)" if omitted else ""
    return f"{'; '.join(parts)}{suffix}"


@dataclass(frozen=True, slots=True)
class WalkEntry:
    kind: WalkKind
    relative: str
    symlink_target: str | None = None


def load_job_state(job_dir: Path) -> JobRetentionState | None:
    """Read ``job-state.json``; missing → None, malformed/symlinked → typed error."""

    try:
        raw = read_file_nofollow(job_dir / JOB_STATE_FILE)
    except StoreViewError as error:
        raise RetentionError(
            "job-state-invalid", f"refusing symlinked job-state file: {error.detail}"
        ) from error
    if raw is None:
        return None
    try:
        state = JobRetentionState.model_validate_json(raw)
    except ValidationError as error:
        raise RetentionError(
            "job-state-invalid",
            f"job state rejected (schema/tamper): {_validation_summary(error)}",
        ) from error
    if state.job_id != job_dir.name:
        raise RetentionError(
            "job-state-invalid",
            f"job_id {state.job_id} does not match directory {job_dir.name}",
        )
    return state


def load_registry(job_dir: Path) -> RetentionRegistry | None:
    """Read the sealed registry; missing → None, malformed/seal-broken → typed error."""

    try:
        raw = read_file_nofollow(job_dir / REGISTRY_FILE)
    except StoreViewError as error:
        raise RetentionError(
            "registry-invalid", f"refusing symlinked registry file: {error.detail}"
        ) from error
    if raw is None:
        return None
    try:
        return RetentionRegistry.model_validate_json(raw)
    except ValidationError as error:
        raise RetentionError(
            "registry-invalid",
            f"retention registry rejected (seal/schema): {_validation_summary(error)}",
        ) from error


def is_within(child_realpath: str, root_realpath: str) -> bool:
    return child_realpath == root_realpath or child_realpath.startswith(root_realpath + os.sep)


def walk_tree(top: Path, root_realpath: str) -> tuple[WalkEntry, ...]:
    """Depth-first, no-follow enumeration of every entry under ``top``.

    Regular directories are containment-checked before descent; symlinks
    are reported without ever being followed; a visited set (keyed by
    realpath) makes directory cycles impossible to hang on.
    """

    entries: list[WalkEntry] = []
    visited: set[str] = {os.path.realpath(top)}
    stack: list[Path] = [top]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as scan:
                children = sorted(scan, key=lambda item: item.name)
        except OSError as error:
            raise RetentionError(
                "traversal-error", f"cannot enumerate {current}: {error}"
            ) from error
        for child in children:
            relative = os.path.relpath(child.path, top).replace(os.sep, "/")
            if child.is_symlink():
                entries.append(
                    WalkEntry(
                        kind="symlink",
                        relative=relative,
                        symlink_target=str(Path(child.path).readlink()),
                    )
                )
                continue
            if child.is_dir(follow_symlinks=False):
                real = os.path.realpath(child.path)
                if not is_within(real, root_realpath):
                    raise RetentionError(
                        "escape-refused",
                        f"directory {child.path} resolves outside the managed root",
                    )
                if real in visited:
                    continue
                visited.add(real)
                entries.append(WalkEntry(kind="dir", relative=relative))
                stack.append(Path(child.path))
            else:
                entries.append(WalkEntry(kind="file", relative=relative))
    return tuple(entries)


__all__ = [
    "WalkEntry",
    "WalkKind",
    "is_within",
    "load_job_state",
    "load_registry",
    "walk_tree",
]
