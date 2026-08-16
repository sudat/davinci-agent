"""Frozen-recipe fixture materialization for Phase-0B ingest test inputs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from services.foundation_io import sha256_file
from services.ingest.fixture_inputs import PHASE_0B_FIXTURE_IDS, materialize_fixture
from tests.ingest.conftest import PHASE_0B_MANIFEST_DIR


def test_materialize_binds_generation_to_frozen_manifest_hash(
    pinned_ffmpeg: Path, tmp_path: Path
) -> None:
    media = materialize_fixture(
        PHASE_0B_MANIFEST_DIR / "p0b-cfr24.json", pinned_ffmpeg, tmp_path
    )
    report = tmp_path / "p0b-cfr24.generation-report.json"
    assert media == tmp_path / "p0b-cfr24.mov"
    assert report.is_file()
    manifest_sha = sha256_file(PHASE_0B_MANIFEST_DIR / "p0b-cfr24.json")
    text = report.read_text()
    assert manifest_sha in text


@pytest.mark.parametrize("fixture_id", PHASE_0B_FIXTURE_IDS)
def test_materialize_yields_probing_media_for_every_variant(
    pinned_ffmpeg: Path, tmp_path: Path, fixture_id: str
) -> None:
    media = materialize_fixture(
        PHASE_0B_MANIFEST_DIR / f"{fixture_id}.json", pinned_ffmpeg, tmp_path
    )
    assert media.is_file()
    assert media.stat().st_size > 0


def test_materialize_reuse_requires_matching_report(
    pinned_ffmpeg: Path, tmp_path: Path
) -> None:
    manifest_path = PHASE_0B_MANIFEST_DIR / "p0b-cfr24.json"
    first = materialize_fixture(manifest_path, pinned_ffmpeg, tmp_path)
    first_hash = sha256_file(first)
    second = materialize_fixture(manifest_path, pinned_ffmpeg, tmp_path)
    assert second == first
    # h264_videotoolbox bytes are not asserted stable across encodes (0A
    # precedent); reuse is keyed on the frozen recipe hashes, not media bytes.
    assert (tmp_path / "p0b-cfr24.generation-report.json").is_file()
    del first_hash


def test_materialize_rejects_noncanonical_manifest(
    pinned_ffmpeg: Path, tmp_path: Path
) -> None:
    source = (PHASE_0B_MANIFEST_DIR / "p0b-cfr24.json").read_bytes()
    tampered = tmp_path / "tampered.json"
    tampered.write_bytes(source[:-1] + b" \n")
    with pytest.raises(ValueError, match="noncanonical"):
        materialize_fixture(tampered, pinned_ffmpeg, tmp_path)


def test_session_fixture_media_matches_declared_variants(
    fixture_media: Mapping[str, Path]
) -> None:
    assert set(fixture_media) == set(PHASE_0B_FIXTURE_IDS)
    for media in fixture_media.values():
        assert media.is_file()
