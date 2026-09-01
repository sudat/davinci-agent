"""Shared fixtures: pinned binaries, frozen lock, six materialized 0B variants."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from services.foundation_io import sha256_file
from services.ingest.fixture_inputs import PHASE_0B_FIXTURE_IDS, materialize_fixture
from services.ingest.ingest import recipe_pointer, register_one
from services.normalize.runner import NormalizeContext, normalize_one
from services.toolchain.models import AnyToolchainLock, Phase0BToolchainLock, load_lock

if TYPE_CHECKING:
    from services.ingest.models import SourceManifest
    from services.normalize.models import NormalizeRecord

PHASE_0B_LOCK = Path("config/toolchains/phase-0b-v2.json")
PHASE_0B_MANIFEST_DIR = Path("tests/fixtures/manifests/phase-0b")


def _pinned(name: str, env_key: str, lock: AnyToolchainLock) -> Path:
    override = os.environ.get(env_key)
    path = Path(override) if override else Path(getattr(lock.ffmpeg, name).path)
    if not path.is_file():
        pytest.skip(f"pinned {name} not bootstrapped: {path}")
    return path


@pytest.fixture(scope="session")
def pinned_ffmpeg() -> Path:
    return _pinned("ffmpeg", "FVP_FFMPEG_BIN", load_lock(PHASE_0B_LOCK))


@pytest.fixture(scope="session")
def pinned_ffprobe() -> Path:
    return _pinned("ffprobe", "FVP_FFPROBE_BIN", load_lock(PHASE_0B_LOCK))


@pytest.fixture(scope="session")
def phase0b_lock() -> Phase0BToolchainLock:
    lock = load_lock(PHASE_0B_LOCK)
    assert isinstance(lock, Phase0BToolchainLock)
    return lock


@pytest.fixture(scope="session")
def fixture_media(
    tmp_path_factory: pytest.TempPathFactory, pinned_ffmpeg: Path
) -> Mapping[str, Path]:
    root = tmp_path_factory.mktemp("p0b-normalize")
    return {
        fixture_id: materialize_fixture(
            PHASE_0B_MANIFEST_DIR / f"{fixture_id}.json", pinned_ffmpeg, root
        )
        for fixture_id in PHASE_0B_FIXTURE_IDS
    }


@pytest.fixture(scope="session")
def source_manifests(
    fixture_media: Mapping[str, Path],
    pinned_ffprobe: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Mapping[str, SourceManifest]:
    """Register each materialized variant once (immutable ingest, todo-21 flow)."""

    root = tmp_path_factory.mktemp("p0b-normalize-manifests")
    pin = Path("config/toolchains/pins/normalize-recipes.json")
    return {
        fixture_id: register_one(
            original=media,
            ffprobe=pinned_ffprobe,
            recipe=recipe_pointer(pin, fixture_id),
            out=root / f"{fixture_id}.source-manifest.json",
        )
        for fixture_id, media in fixture_media.items()
    }


def run_normalize(
    manifest: SourceManifest,
    *,
    ffmpeg: Path,
    ffprobe: Path,
    output_dir: Path,
    record_out: Path,
) -> NormalizeRecord:
    return normalize_one(
        manifest,
        "cfr30",
        NormalizeContext(
            lock_path=PHASE_0B_LOCK,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            output_dir=output_dir,
            record_out=record_out,
        ),
    )


def source_hash(manifest: SourceManifest) -> str:
    return sha256_file(Path(manifest.file.path))
