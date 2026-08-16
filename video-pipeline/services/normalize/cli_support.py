"""Shared CLI helpers: fixture-mode Source Manifest construction."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from services.ingest.fixture_inputs import materialize_fixture
from services.ingest.ingest import register_one
from services.ingest.models import RecipePointer, SourceManifest
from services.normalize.toolchain_guard import load_normalization_section


def lock_recipe_pointer(lock_path: Path, fixture_id: str) -> RecipePointer:
    """Build the recipe pointer from the lock's frozen normalization recipes."""

    section = load_normalization_section(lock_path)
    recipe = next(item for item in section.recipes if item.fixture_id == fixture_id)
    canonical = json.dumps(list(recipe.argv), ensure_ascii=False, separators=(",", ":"))
    return RecipePointer(
        recipe_id=fixture_id,
        recipe_source=str(lock_path),
        args_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def build_source_manifest(arguments: argparse.Namespace) -> SourceManifest:
    """Load ``--source-manifest`` or materialize+register the frozen fixture."""

    if arguments.source_manifest is not None:
        return SourceManifest.model_validate_json(
            arguments.source_manifest.read_bytes()
        )
    if arguments.manifest is None or arguments.fixture_id is None:
        raise ValueError("run requires --source-manifest or --manifest + --fixture-id")
    media_dir = arguments.media_dir or arguments.output_dir
    media = materialize_fixture(arguments.manifest, arguments.ffmpeg, media_dir)
    manifest_out = media_dir / f"{arguments.fixture_id}.source-manifest.json"
    return register_one(
        original=media,
        ffprobe=arguments.ffprobe,
        recipe=lock_recipe_pointer(arguments.lock, arguments.fixture_id),
        out=manifest_out,
    )
