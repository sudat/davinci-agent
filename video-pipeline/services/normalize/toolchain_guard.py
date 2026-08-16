"""Pinned-tool verification and recipe argv construction (never PATH drift).

Both binaries are hash-verified against the frozen Phase-0B lock BEFORE any
execution; the recipe (including its argv template) is read from the lock or
its pins file, never hardcoded here. Final argv is guarded: absolute paths
only, distinct input/output, and any network URL scheme is refused
pre-execution (online relink is out of contract for Edit Mezzanines).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import sha256_file
from services.normalize.errors import (
    NormalizeOnlineRelinkError,
    NormalizeRecipeError,
    NormalizeToolDriftError,
)
from services.toolchain.models import LockError, Phase0BToolchainLock, load_lock
from services.toolchain.normalization import NormalizationSection, NormalizeRecipe

PLACEHOLDER_BINARY = "{ffmpeg}"
PLACEHOLDER_INPUT = "{input}"
PLACEHOLDER_OUTPUT = "{output}"


def verify_pinned_binary(label: str, binary: Path, expected_sha256: str) -> None:
    """Refuse anything but the exact locked binary (absolute path + sha256)."""

    if not binary.is_absolute():
        raise NormalizeToolDriftError(f"{label} path must be absolute: {binary}")
    if not binary.is_file() or sha256_file(binary) != expected_sha256:
        raise NormalizeToolDriftError(f"{label} binary hash drift: {binary}")


def load_phase0b_lock(lock_path: Path) -> Phase0BToolchainLock:
    try:
        lock = load_lock(lock_path)
    except LockError as error:
        raise NormalizeRecipeError(
            f"invalid toolchain lock {lock_path}: {error}"
        ) from error
    if not isinstance(lock, Phase0BToolchainLock):
        raise NormalizeRecipeError(f"lock is not a phase-0b lock: {lock_path}")
    return lock


def load_normalization_section(path: Path) -> NormalizationSection:
    """Load the normalization recipes from a lock file or a pins file.

    Validation goes through the JSON boundary so tuple-typed argv fields
    coerce exactly as they do for the frozen pins file itself.
    """

    try:
        raw = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise NormalizeRecipeError(
            f"cannot read normalization recipes {path}: {error}"
        ) from error
    section_payload = raw.get("normalization", raw) if isinstance(raw, dict) else raw
    try:
        return NormalizationSection.model_validate_json(json.dumps(section_payload))
    except ValidationError as error:
        raise NormalizeRecipeError(
            f"invalid normalization recipes {path}: {error}"
        ) from error


def lookup_recipe(section: NormalizationSection, recipe_id: str) -> NormalizeRecipe:
    for recipe in section.recipes:
        if recipe.fixture_id == recipe_id:
            return recipe
    raise NormalizeRecipeError(f"recipe id is not pinned: {recipe_id}")


def lookup_recipe_from_path(path: Path, recipe_id: str) -> NormalizeRecipe:
    return lookup_recipe(load_normalization_section(path), recipe_id)


def recipe_args_sha256(recipe: NormalizeRecipe) -> str:
    canonical = json.dumps(list(recipe.argv), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _require_absolute(path: Path, role: str) -> Path:
    if not path.is_absolute():
        raise NormalizeOnlineRelinkError(f"{role} path must be absolute: {path}")
    return path


def build_argv(
    recipe_argv: tuple[str, ...],
    *,
    ffmpeg: Path,
    source: Path,
    output: Path,
) -> tuple[str, ...]:
    """Substitute placeholders into the final argv and enforce the IO guards."""

    _require_absolute(ffmpeg, "ffmpeg")
    _require_absolute(source, "input")
    _require_absolute(output, "output")
    if Path(source).resolve() == Path(output).resolve():
        raise NormalizeOnlineRelinkError(
            "input and output paths overlap; originals are never overwritten"
        )
    substitution = {
        PLACEHOLDER_BINARY: str(ffmpeg),
        PLACEHOLDER_INPUT: str(source),
        PLACEHOLDER_OUTPUT: str(output),
    }
    final = tuple(substitution.get(token, token) for token in recipe_argv)
    for token in final:
        if "://" in token:
            raise NormalizeOnlineRelinkError(
                f"recipe argv references a network location and was refused "
                f"pre-execution: {token}"
            )
    return final
