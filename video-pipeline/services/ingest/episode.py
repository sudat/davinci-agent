"""Strict-inclusion multi-file episode registration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.ingest.ingest import IngestError, RecipePointer, register_one


class FileOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    status: Literal["registered", "blocked", "missing_file"]
    reason_codes: tuple[str, ...] = ()
    manifest_path: str | None = None
    sha256: str | None = None


class EpisodeIngestReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    episode_dir: str
    declared: tuple[FileOutcome, ...]
    extras: tuple[str, ...] = Field(default=())


def register_episode(
    *,
    episode_dir: Path,
    declared: tuple[str, ...],
    ffprobe: Path,
    recipe_for: Callable[[str], RecipePointer],
    out_dir: Path,
) -> EpisodeIngestReport:
    """Only declared files are registered; missing files and extras are structured."""

    if not episode_dir.is_dir():
        raise IngestError(f"episode directory is missing: {episode_dir}")
    outcomes: list[FileOutcome] = []
    for name in declared:
        media = episode_dir / name
        if not media.is_file():
            outcomes.append(FileOutcome(name=name, status="missing_file"))
            continue
        out = out_dir / f"{name}.source-manifest.json"
        manifest = register_one(
            original=media, ffprobe=ffprobe, recipe=recipe_for(name), out=out
        )
        outcomes.append(
            FileOutcome(
                name=name,
                status=(
                    "blocked" if manifest.eligibility.verdict == "blocked" else "registered"
                ),
                reason_codes=tuple(reason.code for reason in manifest.eligibility.reasons),
                manifest_path=str(out),
                sha256=manifest.file.sha256,
            )
        )
    declared_names = set(declared)
    extras = tuple(
        sorted(
            entry.name
            for entry in episode_dir.iterdir()
            if entry.name not in declared_names
        )
    )
    return EpisodeIngestReport(
        episode_dir=str(episode_dir.resolve()), declared=tuple(outcomes), extras=extras
    )
