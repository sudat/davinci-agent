"""Migration backend feature flags (PRD 11.x, implementation-plan 2.1).

Strict, frozen, extra-forbidding model plus atomic load/persist helpers that
mirror ``services.config.store`` conventions: ValidationError/OSError are
wrapped as ``BackendsConfigError``, writes go through ``atomic_write`` +
``canonical_model_bytes``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from services.contracts.primitives import StrictModel
from services.foundation_io import atomic_write, canonical_model_bytes

ExecutionBackend = Literal["legacy_direct", "mcp"]
AnalysisBackend = Literal["legacy_local", "mcp", "hybrid"]
EditorialContract = Literal["phase1_v1", "multimodal_v2"]

_VALID_BACKEND_KEYS: frozenset[str] = frozenset(
    {"execution_backend", "analysis_backend", "editorial_contract"}
)

_DEFAULT_BACKENDS_PATH: Path = Path(__file__).resolve().parents[2] / "config" / "backends.json"


class BackendsConfig(StrictModel):
    schema_version: Literal["backends-v1"]
    execution_backend: ExecutionBackend
    analysis_backend: AnalysisBackend
    editorial_contract: EditorialContract


class BackendsConfigError(Exception):
    """The backends config is missing, malformed, or fails validation."""


def load_backends(path: Path | str = _DEFAULT_BACKENDS_PATH) -> BackendsConfig:
    """Load and validate ``backends.json``; fail closed on any drift."""

    resolved = Path(path)
    try:
        raw = resolved.read_bytes()
    except OSError as error:
        raise BackendsConfigError(f"cannot read backends config {resolved}: {error}") from error
    try:
        model = BackendsConfig.model_validate_json(raw)
    except ValidationError as error:
        raise BackendsConfigError(f"invalid backends config {resolved}: {error}") from error
    return model


def set_backend(
    key: str,
    value: str,
    *,
    path: Path | str = _DEFAULT_BACKENDS_PATH,
) -> BackendsConfig:
    """Validate a single backend flag update and persist it atomically.

    Returns the updated in-memory model. The on-disk file is rewritten via
    the project's atomic-write convention, so a failed validation leaves the
    existing file untouched.
    """

    if key not in _VALID_BACKEND_KEYS:
        raise BackendsConfigError(f"unknown backend key: {key!r}")

    resolved = Path(path)
    current = load_backends(resolved)

    try:
        updated = current.model_copy(update={key: value})
        # Re-validate through the model to enforce enum constraints and extra="forbid".
        validated = BackendsConfig.model_validate(updated.model_dump(mode="json"))
    except ValidationError as error:
        raise BackendsConfigError(f"invalid backends value for {key}={value!r}: {error}") from error

    try:
        atomic_write(resolved, canonical_model_bytes(validated))
    except OSError as error:
        raise BackendsConfigError(f"cannot write backends config {resolved}: {error}") from error
    return validated


__all__ = [
    "AnalysisBackend",
    "BackendsConfig",
    "BackendsConfigError",
    "EditorialContract",
    "ExecutionBackend",
    "load_backends",
    "set_backend",
]
