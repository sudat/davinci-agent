"""Snapshot persistence for resolved configuration: atomic write, hash-verified load."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from services.config.models import ResolvedConfig
from services.foundation_io import atomic_write


class ConfigStoreError(Exception):
    """The resolved-config snapshot is missing, malformed, or hash-drifted."""


def save_snapshot(snapshot: ResolvedConfig, path: Path) -> None:
    """Atomically persist the snapshot's canonical bytes."""

    atomic_write(path, snapshot.canonical_bytes())


def load_snapshot(path: Path) -> ResolvedConfig:
    """Load a snapshot and fail closed on any drift from its content hash."""

    try:
        model = ResolvedConfig.model_validate_json(path.read_bytes())
    except OSError as error:
        raise ConfigStoreError(f"cannot read resolved-config snapshot {path}: {error}") from error
    except ValidationError as error:
        raise ConfigStoreError(f"invalid resolved-config snapshot {path}: {error}") from error
    if not model.verify_hash():
        raise ConfigStoreError(
            f"resolved-config snapshot {path} fails its content hash (stale or tampered)"
        )
    return model


__all__ = ["ConfigStoreError", "load_snapshot", "save_snapshot"]
