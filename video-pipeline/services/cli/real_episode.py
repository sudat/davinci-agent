"""The real-episode manifest model and ingest gate (Todo 46).

A real episode is declared by ``episode.json`` in the episode root: the
owner-supplied pointer to actual camera media plus manually declared
privacy/rights flags. The manifest is strict (extra keys refused), the episode
id may NEVER be a frozen Phase-1 fixture id, and ingest reuses the pinned
``register_one`` machinery (hash + bounded packet sampling + sealed Source
Manifest) before eligibility classification runs on PROBED facts.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BeforeValidator, Field

from services.contracts.primitives import Identifier, StrictModel
from services.fixtures.manifest_phase1 import PHASE_1_FIXTURE_IDS
from services.foundation_io import sha256_file
from services.ingest.eligibility import (
    EligibilityDeclared,
    EligibilityResult,
    EpisodeEligibilityBundle,
    classify_episode,
)
from services.ingest.ingest import IngestError, recipe_pointer, register_one
from services.ingest.probe import verify_pinned_ffprobe

if TYPE_CHECKING:
    from services.ingest.models import SourceManifest

EPISODE_MANIFEST_NAME = "episode.json"
NORMALIZE_RECIPES_PIN = Path("config/toolchains/pins/normalize-recipes.json")
CANONICAL_RECIPE_ID = "p0b-cfr24"
FLAG_SEQUENCE = Annotated[tuple[str, ...], BeforeValidator(tuple)]


class RealEpisodeError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class RealEpisodeManifest(StrictModel):
    """Owner-declared real episode: media pointers plus human-gate flags."""

    schema_version: Literal["real-episode-v1"]
    episode_id: Identifier
    video_path: str = Field(min_length=1)
    audio_path: str | None = None
    language: Literal["ja"]
    declared_privacy_flags: FLAG_SEQUENCE = ()
    declared_rights_flags: FLAG_SEQUENCE = ()
    fixture_only: Literal[False]


def load_real_episode(episode_root: Path) -> RealEpisodeManifest:
    path = episode_root / EPISODE_MANIFEST_NAME
    try:
        manifest = RealEpisodeManifest.model_validate_json(path.read_bytes())
    except OSError as error:
        raise RealEpisodeError(
            "episode_manifest_missing", f"cannot read {path}: {error}"
        ) from error
    except ValueError as error:
        raise RealEpisodeError("episode_manifest_invalid", str(error)) from error
    if manifest.episode_id in PHASE_1_FIXTURE_IDS:
        raise RealEpisodeError(
            "fixture_id_refused",
            f"{manifest.episode_id} is a frozen Phase-1 fixture id; the real-episode "
            "path refuses to run frozen fixtures through it",
        )
    return manifest


def resolve_media(manifest: RealEpisodeManifest, episode_root: Path) -> Path:
    video = Path(manifest.video_path)
    if not video.is_absolute():
        video = episode_root / video
    video = video.resolve()
    if not video.is_file():
        raise RealEpisodeError("video_missing", f"declared video does not exist: {video}")
    if manifest.audio_path is not None:
        raise RealEpisodeError(
            "separate_audio_unsupported",
            "a separate audio_path is outside the H1 talking-head contract; declare "
            "audio_path null so the video container's audio is used",
        )
    return video


def register_real_source(video: Path, ffprobe: Path, out: Path) -> SourceManifest:
    """Register the original through the frozen ingest machinery."""

    try:
        recipe = recipe_pointer(NORMALIZE_RECIPES_PIN, CANONICAL_RECIPE_ID)
        return register_one(original=video, ffprobe=ffprobe, recipe=recipe, out=out)
    except (IngestError, LookupError) as error:
        raise RealEpisodeError("ingest_failed", str(error)) from error


def _duration_sec(source: SourceManifest) -> int:
    container = source.container
    return (container.duration_num + container.duration_den - 1) // container.duration_den


def classify_real_episode(
    manifest: RealEpisodeManifest, source: SourceManifest
) -> EligibilityResult:
    videos = sum(1 for stream in source.streams if stream.codec_type == "video")
    audio = any(stream.codec_type == "audio" for stream in source.streams)
    vfr = source.vfr_evidence is not None and source.vfr_evidence.is_vfr
    return classify_episode(
        EpisodeEligibilityBundle(
            episode_id=manifest.episode_id,
            declared=EligibilityDeclared(
                language=manifest.language,
                principal_video_count=videos,
                audio_present=audio,
                vfr=vfr,
                cfr_normalizable=True,
                total_duration_sec=_duration_sec(source),
                speaker_count=1,
                privacy_flags=manifest.declared_privacy_flags,
                rights_flags=manifest.declared_rights_flags,
            ),
        )
    )


def require_runnable(
    manifest: RealEpisodeManifest, eligibility: EligibilityResult
) -> None:
    """Supported status with no open human gates, or a structured stop."""

    if eligibility.human_gates:
        flags = ", ".join(f"{gate.kind}:{gate.flag}" for gate in eligibility.human_gates)
        raise RealEpisodeError(
            "human_gate_open",
            f"declared privacy/rights flags require their human gates first: {flags}",
        )
    if eligibility.status != "supported":
        codes = [reason.code for reason in eligibility.reasons]
        raise RealEpisodeError(
            "episode_not_supported",
            f"{manifest.episode_id} classifies {eligibility.status}: {codes}",
        )


def ingest_real_episode(
    manifest: RealEpisodeManifest, episode_root: Path, ffprobe: Path, out_dir: Path
) -> tuple[SourceManifest, EligibilityResult, str]:
    """Ingest + classify + gate one real episode; returns the sealed source."""

    verify_pinned_ffprobe(ffprobe)
    video = resolve_media(manifest, episode_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = register_real_source(video, ffprobe, out_dir / "source-manifest.json")
    eligibility = classify_real_episode(manifest, source)
    require_runnable(manifest, eligibility)
    return source, eligibility, sha256_file(video)


__all__ = [
    "CANONICAL_RECIPE_ID",
    "EPISODE_MANIFEST_NAME",
    "RealEpisodeError",
    "RealEpisodeManifest",
    "classify_real_episode",
    "ingest_real_episode",
    "load_real_episode",
    "require_runnable",
    "resolve_media",
]
