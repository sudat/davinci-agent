from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from services.ingest.fixture_inputs import PHASE_0B_FIXTURE_IDS, materialize_fixture
from services.toolchain.models import AnyToolchainLock, Phase0BToolchainLock, load_lock
from services.toolchain.normalization import NormalizationSection

PHASE_0B_LOCK = Path("config/toolchains/phase-0b-v2.json")
PHASE_0B_MANIFEST_DIR = Path("tests/fixtures/manifests/phase-0b")
PIN_PATH = Path("config/toolchains/pins/normalize-recipes.json")


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
    """Materialize the six frozen Phase-0B variants once per session.

    Generation is bound to the frozen manifests: each manifest must round-trip
    its own canonical bytes, and the media directory records the manifest and
    generation-argv hashes next to the media.
    """

    root = tmp_path_factory.mktemp("p0b-ingest")
    media: dict[str, Path] = {}
    for fixture_id in PHASE_0B_FIXTURE_IDS:
        manifest_path = PHASE_0B_MANIFEST_DIR / f"{fixture_id}.json"
        media[fixture_id] = materialize_fixture(manifest_path, pinned_ffmpeg, root)
    return media


def recipe_args_sha256(fixture_id: str) -> str:
    """Independently recompute the pinned normalize-recipe argv hash."""

    section = NormalizationSection.model_validate_json(PIN_PATH.read_bytes())
    recipe = next(item for item in section.recipes if item.fixture_id == fixture_id)
    canonical = json.dumps(list(recipe.argv), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
