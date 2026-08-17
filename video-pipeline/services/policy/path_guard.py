"""Realpath allowlist guard for Job and Asset directories (PRD 27).

``resolve_path`` resolves the candidate with symlink-resolving realpath and
only returns it when the result stays under an allowlisted root. ``..``
components are refused outright before resolution. Scope: a single-host spike
guard — this is structural path validation at check time; no TOCTOU
(time-of-check-to-time-of-use) protection is claimed for concurrent writers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.config.models import PathAllowlist


class PathPolicyError(ValueError):
    """The candidate path is not reachable inside the allowlist."""


def resolve_path(candidate: str | Path, allowlist: PathAllowlist) -> Path:
    """Return the realpath of ``candidate`` or refuse any escape."""

    requested = Path(candidate)
    if not requested.is_absolute():
        raise PathPolicyError(f"candidate path must be absolute: {candidate}")
    if ".." in requested.parts:
        raise PathPolicyError(f"candidate path contains '..' traversal: {candidate}")
    real = Path(os.path.realpath(requested))
    for root in allowlist.roots:
        real_root = Path(os.path.realpath(root))
        if real == real_root or real.is_relative_to(real_root):
            return real
    raise PathPolicyError(f"resolved path '{real}' is outside every allowlisted root")


__all__ = ["PathPolicyError", "resolve_path"]
