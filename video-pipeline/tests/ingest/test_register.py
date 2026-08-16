"""Happy-path registration: the six frozen Phase-0B variants."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from services.foundation_io import sha256_file
from services.ingest.commit import verify_manifest_hash
from services.ingest.episode import register_episode
from services.ingest.ingest import recipe_pointer, register_one
from services.ingest.models import AudioStreamRecord, SourceManifest, VideoStreamRecord
from tests.ingest.conftest import (
    PHASE_0B_MANIFEST_DIR,
    PIN_PATH,
    recipe_args_sha256,
)

EXPECTED = {
    fixture_id: json.loads((PHASE_0B_MANIFEST_DIR / f"{fixture_id}.json").read_bytes())[
        "expected_ffprobe"
    ]
    for fixture_id in (
        "p0b-cfr24",
        "p0b-ntsc2997",
        "p0b-ntsc5994",
        "p0b-vfr-2-3-cadence",
        "p0b-rotate90",
        "p0b-audio-offset1024",
    )
}


def _register(
    media: Path, fixture_id: str, pinned_ffprobe: Path, tmp_path: Path
) -> tuple[SourceManifest, Path]:
    out = tmp_path / f"{fixture_id}.source-manifest.json"
    recipe = recipe_pointer(PIN_PATH, fixture_id)
    manifest = register_one(original=media, ffprobe=pinned_ffprobe, recipe=recipe, out=out)
    return manifest, out


def _video(manifest: SourceManifest) -> VideoStreamRecord:
    stream = next(s for s in manifest.streams if s.codec_type == "video")
    assert isinstance(stream, VideoStreamRecord)
    return stream


def _audio(manifest: SourceManifest) -> AudioStreamRecord:
    stream = next(s for s in manifest.streams if s.codec_type == "audio")
    assert isinstance(stream, AudioStreamRecord)
    return stream


@pytest.mark.parametrize("fixture_id", list(EXPECTED))
def test_variant_registers_supported_against_frozen_expectations(
    fixture_media: Mapping[str, Path],
    pinned_ffprobe: Path,
    tmp_path: Path,
    fixture_id: str,
) -> None:
    manifest, out = _register(fixture_media[fixture_id], fixture_id, pinned_ffprobe, tmp_path)
    expected = EXPECTED[fixture_id]
    assert manifest.eligibility.verdict == "supported"
    assert manifest.eligibility.reasons == ()
    assert verify_manifest_hash(manifest) is True
    assert out.is_file()

    video = _video(manifest)
    r_num, r_den = map(int, expected["video"]["r_frame_rate"].split("/"))
    a_num, a_den = map(int, expected["video"]["avg_frame_rate"].split("/"))
    assert (video.r_frame_rate_num, video.r_frame_rate_den) == (r_num, r_den)
    assert (video.avg_frame_rate_num, video.avg_frame_rate_den) == (a_num, a_den)
    assert video.nb_frames == int(expected["video"]["nb_frames"])
    assert video.width == expected["video"]["width"]
    assert video.height == expected["video"]["height"]
    assert video.pix_fmt == expected["video"]["pix_fmt"]

    audio = _audio(manifest)
    assert audio.codec_name == expected["audio"]["codec_name"]
    assert audio.sample_rate == int(expected["audio"]["sample_rate"])
    assert audio.channels == expected["audio"]["channels"]

    duration = expected["format"]["duration_seconds"]
    assert manifest.container.duration_num == duration["num"]
    assert manifest.container.duration_den == duration["den"]

    assert all(entry.monotonic for entry in manifest.monotonicity)
    assert manifest.edit_source_recipe.recipe_id == fixture_id


def test_originals_are_never_mutated_by_full_ingest(
    fixture_media: Mapping[str, Path], pinned_ffprobe: Path, tmp_path: Path
) -> None:
    media = fixture_media["p0b-cfr24"]
    before = sha256_file(media)
    size_before = media.stat().st_size
    manifest, _out = _register(media, "p0b-cfr24", pinned_ffprobe, tmp_path)
    assert sha256_file(media) == before
    assert media.stat().st_size == size_before
    assert manifest.file.sha256 == before
    assert manifest.file.size_bytes == size_before


def test_cfr_variants_report_single_delta_class(
    fixture_media: Mapping[str, Path], pinned_ffprobe: Path, tmp_path: Path
) -> None:
    for fixture_id in ("p0b-cfr24", "p0b-ntsc2997", "p0b-ntsc5994"):
        manifest, _ = _register(fixture_media[fixture_id], fixture_id, pinned_ffprobe, tmp_path)
        evidence = manifest.vfr_evidence
        assert evidence is not None
        assert evidence.is_vfr is False
        assert len(evidence.delta_classes) == 1


def test_vfr_cadence_reports_two_exact_delta_classes(
    fixture_media: Mapping[str, Path], pinned_ffprobe: Path, tmp_path: Path
) -> None:
    manifest, _ = _register(
        fixture_media["p0b-vfr-2-3-cadence"], "p0b-vfr-2-3-cadence", pinned_ffprobe, tmp_path
    )
    evidence = manifest.vfr_evidence
    assert evidence is not None
    assert evidence.is_vfr is True
    assert {delta.delta_ticks for delta in evidence.delta_classes} == {2000, 3000}
    assert evidence.time_base_den == 60000
    assert sum(delta.count for delta in evidence.delta_classes) == 120
    assert evidence.sampled_packets == 121


def test_rotate90_reports_display_matrix_rotation(
    fixture_media: Mapping[str, Path], pinned_ffprobe: Path, tmp_path: Path
) -> None:
    manifest, _ = _register(fixture_media["p0b-rotate90"], "p0b-rotate90", pinned_ffprobe, tmp_path)
    assert _video(manifest).rotation_degrees == 90


def test_audio_offset_variant_records_exact_audio_position(
    fixture_media: Mapping[str, Path], pinned_ffprobe: Path, tmp_path: Path
) -> None:
    manifest, _ = _register(
        fixture_media["p0b-audio-offset1024"], "p0b-audio-offset1024", pinned_ffprobe, tmp_path
    )
    audio = _audio(manifest)
    assert audio.start_offset_samples == 0
    assert (audio.duration_num, audio.duration_den) == (20, 1)
    assert manifest.eligibility.verdict == "supported"


def test_recipe_pointer_hashes_frozen_pin_argv(tmp_path: Path) -> None:
    pointer = recipe_pointer(PIN_PATH, "p0b-cfr24")
    assert pointer.recipe_source == str(PIN_PATH)
    assert pointer.args_sha256 == recipe_args_sha256("p0b-cfr24")
    with pytest.raises(LookupError, match="p0b-unknown"):
        recipe_pointer(PIN_PATH, "p0b-unknown")


def test_register_episode_strict_inclusion(
    fixture_media: Mapping[str, Path], pinned_ffprobe: Path, tmp_path: Path
) -> None:
    episode = tmp_path / "episode"
    episode.mkdir()
    (episode / "a.mov").write_bytes(fixture_media["p0b-vfr-2-3-cadence"].read_bytes())
    (episode / "undeclared.txt").write_bytes(b"stray")
    report = register_episode(
        episode_dir=episode,
        declared=("a.mov", "missing.mov"),
        ffprobe=pinned_ffprobe,
        recipe_for=lambda name: recipe_pointer(PIN_PATH, "p0b-vfr-2-3-cadence"),
        out_dir=tmp_path / "manifests",
    )
    statuses = {outcome.name: outcome.status for outcome in report.declared}
    assert statuses["a.mov"] == "registered"
    assert statuses["missing.mov"] == "missing_file"
    assert report.extras == ("undeclared.txt",)
    assert (tmp_path / "manifests" / "a.mov.source-manifest.json").is_file()
