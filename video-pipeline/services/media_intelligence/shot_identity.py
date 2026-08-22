"""Deterministic shot identity derivation.

Same ``(source_id, span, provider)`` tuple always yields the same
``shot_id`` across runs, without timestamps, random or uuid.  The
derivation is a stable hash of the canonical input string.

Validation against the Conform Map world:
    a shot referencing a ``source_id`` absent from the conform context
    is a typed rejection (``UnknownSourceError``), never silent.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

_IDENTIFIER_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class ShotIdentityError(ValueError):
    """Base shot identity failure."""

    LABEL = "shot_identity_error"


class UnknownSourceError(ShotIdentityError):
    """A shot references a source_id absent from the conform context."""

    LABEL = "unknown_source"


def _validate_identifier(value: str, label: str) -> None:
    if not _IDENTIFIER_RE.match(value):
        raise ShotIdentityError(f"{label} must match Identifier pattern: {value!r}")


def derive_shot_id(
    source_id: str,
    start_frame: int,
    end_frame: int,
    provider: str,
    provider_version: str | None = None,
) -> str:
    """Derive a deterministic shot identifier.

    Canonical input ``f"{source_id}:{start_frame}:{end_frame}:{provider}:{version}"``
    is hashed with SHA-256; the first 16 hex characters are used as
    ``shot-<hex>``.  The result satisfies ``Identifier``.
    """

    _validate_identifier(source_id, "source_id")
    _validate_identifier(provider, "provider")
    if provider_version is not None:
        _validate_identifier(provider_version, "provider_version")
    if not isinstance(start_frame, int) or not isinstance(end_frame, int):
        raise ShotIdentityError("frames must be strict integers")
    if start_frame < 0 or end_frame < 0:
        raise ShotIdentityError("frames must be >= 0")
    if end_frame < start_frame:
        raise ShotIdentityError("end_frame must be >= start_frame")

    canonical = f"{source_id}:{start_frame}:{end_frame}:{provider}:{provider_version or ''}"
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    shot_id = f"shot-{digest}"
    if not _IDENTIFIER_RE.match(shot_id):
        raise ShotIdentityError(f"derived shot_id invalid: {shot_id!r}")
    return shot_id


def ensure_source_known(source_id: str, allowed_ids: set[str] | frozenset[str]) -> None:
    """Raise ``UnknownSourceError`` when ``source_id`` is not allowed."""

    if source_id not in allowed_ids:
        raise UnknownSourceError(f"unknown source_id: {source_id!r}")
