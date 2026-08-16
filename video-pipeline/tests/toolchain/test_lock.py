from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from services.foundation_io import sha256_file
from services.toolchain.models import (
    BinaryRecord,
    LockError,
    SourceRecord,
    ToolchainLock,
    load_lock,
)
from services.toolchain.verify import verify_binary

FROZEN_CONFIGURE_ARGV = (
    "--disable-doc",
    "--disable-debug",
    "--disable-network",
    "--disable-autodetect",
    "--enable-avcodec",
    "--enable-avformat",
    "--enable-avfilter",
    "--enable-swresample",
    "--enable-swscale",
    "--enable-videotoolbox",
    "--enable-audiotoolbox",
    "--enable-protocol=file",
    "--enable-indev=lavfi",
    "--enable-demuxer=mov,matroska",
    "--enable-muxer=mp4,mov",
    "--enable-decoder=h264,hevc,aac,pcm_s16le",
    "--enable-encoder=h264_videotoolbox,aac,pcm_s16le",
    "--enable-filter=testsrc2,color,scale,fps,aresample,asetpts,setpts,overlay",
)


def _binary_record(path: Path) -> dict[str, str]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path.resolve()),
        "version_output": "fixture version",
    }


def test_binary_drift_is_rejected(tmp_path: Path) -> None:
    binary = tmp_path / "ffmpeg"
    binary.write_bytes(Path(sys.executable).read_bytes())
    record = BinaryRecord.model_validate(_binary_record(binary))
    binary.write_bytes(binary.read_bytes() + b"drift")

    with pytest.raises(LockError, match="binary hash drift"):
        verify_binary(record)


def test_missing_provenance_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Field required"):
        ToolchainLock.model_validate(
            {
                "schema_version": "phase-toolchain-lock-v1",
                "phase": "phase-0a",
            }
        )


def test_noncanonical_lock_is_rejected(tmp_path: Path) -> None:
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"phase": "phase-0a"}, indent=2))

    with pytest.raises(LockError, match=r"invalid toolchain lock|noncanonical"):
        load_lock(lock)


def test_malformed_lock_json_is_rejected(tmp_path: Path) -> None:
    lock = tmp_path / "lock.json"
    lock.write_text('{"phase":')

    with pytest.raises(LockError, match="invalid toolchain lock"):
        load_lock(lock)


def test_phase0a_lock_preserves_frozen_build_and_passed_smokes() -> None:
    lock = load_lock(Path("config/toolchains/phase-0a-v1.json"))

    assert lock.ffmpeg.build.configure_argv == FROZEN_CONFIGURE_ARGV
    assert lock.ffmpeg.build.install_method == "direct-binary-copy"
    assert all(path.startswith("/") for path in lock.ffmpeg.build.build_source_paths)
    assert lock.ffmpeg.source.tarball_sha256 == (
        "733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1"
    )
    assert lock.smoke.resolve_readonly.status == "passed"
    assert lock.smoke.ffmpeg_probe.status == "passed"


def test_relative_source_tarball_path_is_rejected() -> None:
    with pytest.raises(ValidationError, match="source tarball path must be absolute"):
        SourceRecord(
            url="https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz",
            tag="n7.1.1",
            tarball_path="bootstrap/ffmpeg-7.1.1.tar.xz",
            tarball_sha256=(
                "733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1"
            ),
        )
