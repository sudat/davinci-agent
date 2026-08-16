"""Failure paths: drift, stale state, metadata loss, frame accounting, security."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

import services.normalize.runner as runner_module
from services.normalize.errors import (
    NormalizeOnlineRelinkError,
    NormalizeRecipeError,
    NormalizeToolDriftError,
    NormalizeVerificationError,
)
from services.normalize.models import NormalizeRecord, verify_normalize_record
from services.normalize.runner import NormalizeContext, normalize_one
from services.normalize.toolchain_guard import build_argv, lookup_recipe_from_path
from tests.normalize.conftest import PHASE_0B_LOCK, run_normalize, source_hash

if TYPE_CHECKING:
    from services.conform.rate_model import CfrConversionReport
    from services.contracts.primitives import RationalFrameRate
    from services.ingest.models import SourceManifest
    from services.normalize.accounting import SourceSpan
    from services.toolchain.normalization import NormalizationSection, NormalizeRecipe


def _drifted_copy(binary: Path, destination: Path) -> Path:
    shutil.copyfile(binary, destination)
    with destination.open("ab") as stream:
        stream.write(b"\x00drift")
    return destination


def test_refuses_drifted_ffmpeg_before_any_execution(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    drifted = _drifted_copy(pinned_ffmpeg, tmp_path / "drifted-ffmpeg")
    output_dir = tmp_path / "edit-sources"
    with pytest.raises(NormalizeToolDriftError, match="ffmpeg"):
        run_normalize(
            source_manifests["p0b-cfr24"],
            ffmpeg=drifted,
            ffprobe=pinned_ffprobe,
            output_dir=output_dir,
            record_out=tmp_path / "r.json",
        )
    assert not output_dir.exists()
    assert not (tmp_path / "r.json").exists()


def test_refuses_drifted_ffprobe_before_any_execution(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    drifted = _drifted_copy(pinned_ffprobe, tmp_path / "drifted-ffprobe")
    output_dir = tmp_path / "edit-sources"
    with pytest.raises(NormalizeToolDriftError, match="ffprobe"):
        run_normalize(
            source_manifests["p0b-cfr24"],
            ffmpeg=pinned_ffmpeg,
            ffprobe=drifted,
            output_dir=output_dir,
            record_out=tmp_path / "r.json",
        )
    assert not output_dir.exists()


def test_refuses_relative_binary_paths(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(NormalizeToolDriftError, match="absolute"):
        run_normalize(
            source_manifests["p0b-cfr24"],
            ffmpeg=Path("ffmpeg"),
            ffprobe=pinned_ffprobe,
            output_dir=tmp_path / "o",
            record_out=tmp_path / "r.json",
        )


def test_refuses_stale_source_manifest(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    fixture_media: Mapping[str, Path],
    tmp_path: Path,
) -> None:
    stale_copy = tmp_path / "stale.mov"
    shutil.copyfile(fixture_media["p0b-cfr24"], stale_copy)
    with stale_copy.open("ab") as stream:
        stream.write(b"mutated-after-registration")
    manifest = source_manifests["p0b-cfr24"]
    stale = manifest.model_copy(
        update={
            "file": manifest.file.model_copy(update={"path": str(stale_copy.resolve())})
        }
    )
    with pytest.raises(NormalizeVerificationError) as error:
        run_normalize(
            stale,
            ffmpeg=pinned_ffmpeg,
            ffprobe=pinned_ffprobe,
            output_dir=tmp_path / "o",
            record_out=tmp_path / "r.json",
        )
    assert error.value.reason_code == "stale_source_manifest"


def test_unexplained_frame_count_is_a_hard_failure(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = runner_module.expected_conversion

    def off_by_one(span: SourceSpan, target: RationalFrameRate) -> CfrConversionReport:
        report = real(span, target)
        return report.model_copy(update={"output_frames": report.output_frames + 1})

    monkeypatch.setattr(runner_module, "expected_conversion", off_by_one)
    with pytest.raises(NormalizeVerificationError) as error:
        run_normalize(
            source_manifests["p0b-cfr24"],
            ffmpeg=pinned_ffmpeg,
            ffprobe=pinned_ffprobe,
            output_dir=tmp_path / "o",
            record_out=tmp_path / "r.json",
        )
    assert error.value.reason_code == "unexplained_frame_count"
    assert "unexplained frame count" in str(error.value)
    assert not (tmp_path / "r.json").exists()


def test_silent_metadata_loss_is_blocked(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = runner_module.lookup_recipe
    pointer_hash = source_manifests["p0b-cfr24"].edit_source_recipe.args_sha256
    monkeypatch.setattr(
        runner_module, "recipe_args_sha256", lambda recipe: pointer_hash
    )

    def audio_dropping_recipe(
        section: NormalizationSection, recipe_id: str
    ) -> NormalizeRecipe:
        recipe = real(section, recipe_id)
        argv = list(recipe.argv)
        index = argv.index("-c:a")
        argv[index : index + 2] = ["-an"]
        return recipe.model_copy(update={"argv": tuple(argv)})

    monkeypatch.setattr(runner_module, "lookup_recipe", audio_dropping_recipe)
    with pytest.raises(NormalizeVerificationError) as error:
        run_normalize(
            source_manifests["p0b-cfr24"],
            ffmpeg=pinned_ffmpeg,
            ffprobe=pinned_ffprobe,
            output_dir=tmp_path / "o",
            record_out=tmp_path / "r.json",
        )
    assert error.value.reason_code == "silent_metadata_loss"
    assert "audio" in str(error.value)
    assert not (tmp_path / "r.json").exists()


def test_online_relink_argv_is_refused_pre_execution(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = runner_module.lookup_recipe
    pointer_hash = source_manifests["p0b-cfr24"].edit_source_recipe.args_sha256
    monkeypatch.setattr(
        runner_module, "recipe_args_sha256", lambda recipe: pointer_hash
    )

    def remote_recipe(section: NormalizationSection, recipe_id: str) -> NormalizeRecipe:
        recipe = real(section, recipe_id)
        argv = list(recipe.argv)
        argv[argv.index("{input}")] = "http://media.internal.example.com/p0b-cfr24.mov"
        return recipe.model_copy(update={"argv": tuple(argv)})

    monkeypatch.setattr(runner_module, "lookup_recipe", remote_recipe)
    output_dir = tmp_path / "o"
    with pytest.raises(NormalizeOnlineRelinkError):
        run_normalize(
            source_manifests["p0b-cfr24"],
            ffmpeg=pinned_ffmpeg,
            ffprobe=pinned_ffprobe,
            output_dir=output_dir,
            record_out=tmp_path / "r.json",
        )
    assert not output_dir.exists()
    assert source_hash(source_manifests["p0b-cfr24"]) == source_manifests[
        "p0b-cfr24"
    ].file.sha256


RECIPE_ARGV: tuple[str, ...] = (
    "{ffmpeg}", "-v", "error", "-i", "{input}", "-c:v", "h264_videotoolbox",
    "-c:a", "pcm_s16le", "-y", "{output}",
)


def test_build_argv_refuses_every_network_scheme(
    pinned_ffmpeg: Path, tmp_path: Path
) -> None:
    for url in (
        "http://example.com/a.mov",
        "https://example.com/a.mov",
        "rtsp://camera/stream",
        "tcp://host:1234",
        "file:///tmp/a.mov",
    ):
        argv = list(RECIPE_ARGV)
        argv[argv.index("{input}")] = url
        with pytest.raises(NormalizeOnlineRelinkError, match="network"):
            build_argv(
                tuple(argv),
                ffmpeg=pinned_ffmpeg,
                source=tmp_path / "source.mov",
                output=tmp_path / "out.mov",
            )


def test_build_argv_requires_absolute_io_paths_and_distinct_output(
    pinned_ffmpeg: Path, tmp_path: Path
) -> None:
    source = tmp_path / "source.mov"
    source.write_bytes(b"0")
    with pytest.raises(NormalizeOnlineRelinkError, match="absolute"):
        build_argv(
            RECIPE_ARGV,
            ffmpeg=pinned_ffmpeg,
            source=Path("relative.mov"),
            output=tmp_path / "out.mov",
        )
    with pytest.raises(NormalizeOnlineRelinkError, match="overlap"):
        build_argv(
            RECIPE_ARGV,
            ffmpeg=pinned_ffmpeg,
            source=source.resolve(),
            output=source.resolve(),
        )


def test_output_source_hash_confusion_is_rejected_by_record_validation(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    record = run_normalize(
        source_manifests["p0b-cfr24"],
        ffmpeg=pinned_ffmpeg,
        ffprobe=pinned_ffprobe,
        output_dir=tmp_path / "o",
        record_out=tmp_path / "r.json",
    )
    confused = record.model_copy(
        update={
            "output": record.output.model_copy(update={"sha256": record.source.sha256})
        }
    )
    with pytest.raises(ValidationError, match="hash_confusion"):
        NormalizeRecord.model_validate_json(confused.model_dump_json())

    lying = record.model_copy(
        update={
            "output": record.output.model_copy(
                update={"sha256": "0" * 63 + "1", "size_bytes": record.source.size_bytes}
            )
        }
    )
    assert not verify_normalize_record(lying)


def test_malformed_recipe_json_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(NormalizeRecipeError):
        lookup_recipe_from_path(missing, "p0b-cfr24")
    bad = tmp_path / "bad-recipes.json"
    bad.write_text('{"schema_version": "normalize-recipes-v1", "broken": true}')
    with pytest.raises(NormalizeRecipeError):
        lookup_recipe_from_path(bad, "p0b-cfr24")


def test_unknown_recipe_id_is_refused() -> None:
    with pytest.raises(NormalizeRecipeError, match="not pinned"):
        lookup_recipe_from_path(PHASE_0B_LOCK, "p0b-unknown")


def test_target_profile_must_match_the_frozen_lock(
    source_manifests: Mapping[str, SourceManifest],
    pinned_ffmpeg: Path,
    pinned_ffprobe: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(NormalizeRecipeError, match="target"):
        normalize_one(
            source_manifests["p0b-cfr24"],
            "cfr24",
            NormalizeContext(
                lock_path=PHASE_0B_LOCK,
                ffmpeg=pinned_ffmpeg,
                ffprobe=pinned_ffprobe,
                output_dir=tmp_path / "o",
                record_out=tmp_path / "r.json",
            ),
        )
