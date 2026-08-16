"""Happy-path normalization: CFR Edit Mezzanines from the frozen 0B variants."""

from __future__ import annotations

import json
import subprocess
from array import array
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from services.foundation_io import sha256_file
from services.normalize.models import (
    NormalizeRecord,
    verify_normalize_record,
    verify_normalize_record_hash,
)
from services.normalize.toolchain_guard import load_normalization_section
from tests.normalize.conftest import PHASE_0B_LOCK, PHASE_0B_MANIFEST_DIR, run_normalize

if TYPE_CHECKING:
    from services.ingest.models import SourceManifest
    from services.toolchain.models import Phase0BToolchainLock

FIXTURE_ORDER = (
    "p0b-cfr24",
    "p0b-ntsc2997",
    "p0b-ntsc5994",
    "p0b-vfr-2-3-cadence",
    "p0b-rotate90",
    "p0b-audio-offset1024",
)


def _golden(fixture_id: str) -> dict[str, object]:
    payload = json.loads((PHASE_0B_MANIFEST_DIR / f"{fixture_id}.json").read_bytes())
    golden = payload["conversions"]["cfr30"]
    assert isinstance(golden, dict)
    return golden


def _normalize(
    manifests: Mapping[str, SourceManifest],
    fixture_id: str,
    ffmpeg: Path,
    ffprobe: Path,
    tmp_path: Path,
) -> tuple[NormalizeRecord, Path]:
    record_out = tmp_path / "records" / f"{fixture_id}.normalize-record.json"
    record = run_normalize(
        manifests[fixture_id],
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        output_dir=tmp_path / "edit-sources",
        record_out=record_out,
    )
    return record, record_out


def _assert_golden_accounting(record: NormalizeRecord, fixture_id: str) -> None:
    golden = _golden(fixture_id)
    expected = record.drop_dup.expected
    assert expected.output_frames == golden["output_frames"]
    assert list(expected.dropped) == golden["dropped_source_frames"]
    assert list(expected.duplicated) == golden["duplicated_source_frames"]
    assert record.drop_dup.basis == "conform-frame-conversion-accounting-v1"
    assert record.output_semantics.observed_output_frames == expected.output_frames


def test_cfr24_passthrough_class_has_no_drops(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    record, _ = _normalize(
        source_manifests, "p0b-cfr24", pinned_ffmpeg, pinned_ffprobe, tmp_path
    )
    assert record.drop_dup.expected.dropped == ()
    assert record.drop_dup.expected.output_frames == 750
    assert record.drop_dup.expected.duplicated[0] == 1
    assert record.drop_dup.expected.duplicated[-1] == 597
    _assert_golden_accounting(record, "p0b-cfr24")


def test_ntsc2997_duplicates_frame_499_into_601(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    record, _ = _normalize(
        source_manifests, "p0b-ntsc2997", pinned_ffmpeg, pinned_ffprobe, tmp_path
    )
    assert record.drop_dup.expected.dropped == ()
    assert list(record.drop_dup.expected.duplicated) == [499]
    assert record.drop_dup.expected.output_frames == 601
    _assert_golden_accounting(record, "p0b-ntsc2997")


def test_vfr_cadence_matches_frozen_golden_table(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    record, _ = _normalize(
        source_manifests,
        "p0b-vfr-2-3-cadence",
        pinned_ffmpeg,
        pinned_ffprobe,
        tmp_path,
    )
    assert record.drop_dup.expected.dropped == ()
    assert list(record.drop_dup.expected.duplicated) == [
        index for index in range(120) if index % 4 == 1
    ]
    assert record.drop_dup.expected.output_frames == 150
    _assert_golden_accounting(record, "p0b-vfr-2-3-cadence")


def test_rotate90_output_keeps_coded_pixels_and_rotation_metadata(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    record, _ = _normalize(
        source_manifests, "p0b-rotate90", pinned_ffmpeg, pinned_ffprobe, tmp_path
    )
    assert record.policy.rotation == "noautorotate-rotation-metadata-preserved-v1"
    assert record.drop_dup.expected.output_frames == 600
    _assert_golden_accounting(record, "p0b-rotate90")


def test_audio_offset1024_preserves_48khz_and_content_offset(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    record, _ = _normalize(
        source_manifests,
        "p0b-audio-offset1024",
        pinned_ffmpeg,
        pinned_ffprobe,
        tmp_path,
    )
    assert record.target.sample_rate == 48000
    raw = subprocess.run(
        [
            str(pinned_ffmpeg), "-v", "error", "-i", record.output.path,
            "-map", "0:a:0", "-f", "s16le", "-c:a", "pcm_s16le", "-",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    ).stdout
    samples = array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    onsets: list[int] = []
    in_pulse = False
    for index, sample in enumerate(samples):
        if sample != 0 and not in_pulse:
            onsets.append(index)
            in_pulse = True
        elif sample == 0:
            in_pulse = False
    assert onsets == [241024, 481024, 721024]


def test_every_variant_matches_its_frozen_golden_conversion(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    for fixture_id in FIXTURE_ORDER:
        record, _ = _normalize(
            source_manifests, fixture_id, pinned_ffmpeg, pinned_ffprobe, tmp_path
        )
        _assert_golden_accounting(record, fixture_id)


def test_record_is_strict_envelope_bound_to_lock_and_source(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    phase0b_lock: Phase0BToolchainLock,
    tmp_path: Path,
) -> None:
    manifest = source_manifests["p0b-ntsc2997"]
    record, record_out = _normalize(
        source_manifests, "p0b-ntsc2997", pinned_ffmpeg, pinned_ffprobe, tmp_path
    )
    assert record.schema_version == "normalize-record-v1"
    assert record.artifact_type == "normalize-record"
    assert record.producer.name == "services.normalize"
    assert record.inputs[0].artifact_id == manifest.artifact_id
    assert record.inputs[0].sha256 == manifest.content_hash
    assert record.tool.ffmpeg_sha256 == phase0b_lock.ffmpeg.ffmpeg.sha256
    assert record.tool.ffprobe_sha256 == phase0b_lock.ffmpeg.ffprobe.sha256
    assert record.tool.lock_sha256 == sha256_file(PHASE_0B_LOCK)
    assert record.argv[0] == str(pinned_ffmpeg.resolve())
    assert record.target.frame_rate.num == 30
    assert record.target.frame_rate.den == 1
    assert record.target.sample_rate == 48000
    assert record.target.video_codec == "h264_videotoolbox"
    assert record.target.audio_codec == "pcm_s16le"
    assert record.policy.color == "preserve-or-explicit-v1"

    def _assert_float_free(node: object) -> None:
        assert not isinstance(node, float)
        if isinstance(node, dict):
            for value in node.values():
                _assert_float_free(value)
        elif isinstance(node, list):
            for value in node:
                _assert_float_free(value)

    _assert_float_free(json.loads(record.model_dump_json()))

    round_trip = NormalizeRecord.model_validate_json(record_out.read_bytes())
    assert round_trip == record
    assert verify_normalize_record_hash(round_trip)
    assert verify_normalize_record(round_trip)


def test_output_goes_to_new_directory_and_source_is_immutable(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    manifest = source_manifests["p0b-cfr24"]
    before = sha256_file(Path(manifest.file.path))
    record, _ = _normalize(
        source_manifests, "p0b-cfr24", pinned_ffmpeg, pinned_ffprobe, tmp_path
    )
    output = Path(record.output.path)
    assert output.is_file()
    assert tmp_path in output.parents
    assert output.resolve() != Path(manifest.file.path).resolve()
    assert sha256_file(Path(manifest.file.path)) == before
    assert record.source.sha256 == before
    assert record.source.size_bytes == Path(manifest.file.path).stat().st_size
    assert record.output.sha256 == sha256_file(output)
    assert record.output.size_bytes == output.stat().st_size


def test_replay_is_semantically_equivalent_not_byte_identical(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    first, _ = _normalize(
        source_manifests, "p0b-ntsc2997", pinned_ffmpeg, pinned_ffprobe, tmp_path
    )
    second = run_normalize(
        source_manifests["p0b-ntsc2997"],
        ffmpeg=pinned_ffmpeg,
        ffprobe=pinned_ffprobe,
        output_dir=tmp_path / "edit-sources",
        record_out=tmp_path / "records" / "replay.normalize-record.json",
    )
    assert first.argv == second.argv
    assert first.source == second.source
    assert first.drop_dup == second.drop_dup
    assert first.target == second.target
    assert first.replay.determinism == "semantic-equivalence-h264-videotoolbox"
    assert (
        first.output_semantics.decoded_video_sha256
        == second.output_semantics.decoded_video_sha256
    )
    assert first.output.sha256 != second.output.sha256


def test_recipe_argv_comes_from_lock_not_hardcoded(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    record, _ = _normalize(
        source_manifests, "p0b-rotate90", pinned_ffmpeg, pinned_ffprobe, tmp_path
    )
    section = load_normalization_section(PHASE_0B_LOCK)
    recipe = next(item for item in section.recipes if item.fixture_id == "p0b-rotate90")
    substitution = {
        "{ffmpeg}": str(pinned_ffmpeg.resolve()),
        "{input}": record.source.path,
        "{output}": record.output.path,
    }
    assert list(record.argv) == [substitution.get(token, token) for token in recipe.argv]
    assert "-noautorotate" in record.argv
