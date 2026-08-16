from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services.contracts.primitives import Sha256
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file

if TYPE_CHECKING:
    from services.resolve_bridge.models import ResolveHostReport

Sha256Value = Sha256
FROZEN_CONFIGURE_ARGV: Final = (
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


class LockError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BinaryRecord(StrictModel):
    path: str
    sha256: Sha256Value
    version_output: str

    @field_validator("path")
    @classmethod
    def require_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("binary path must be absolute")
        return value


class PythonToolchain(StrictModel):
    uv: BinaryRecord
    python: BinaryRecord


class SourceRecord(StrictModel):
    url: Literal["https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz"]
    tag: Literal["n7.1.1"]
    tarball_path: str
    tarball_sha256: Literal["733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1"]

    @field_validator("tarball_path")
    @classmethod
    def require_absolute_tarball_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("source tarball path must be absolute")
        return value


class BuildRecord(StrictModel):
    configure_argv: tuple[str, ...] = Field(min_length=1)
    compiler: str
    install_method: Literal["direct-binary-copy"]
    build_source_paths: tuple[str, ...] = Field(min_length=1)

    @field_validator("build_source_paths")
    @classmethod
    def require_absolute_build_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not Path(path).is_absolute() for path in value):
            raise ValueError("build source paths must be absolute")
        return value


class FfmpegToolchain(StrictModel):
    source: SourceRecord
    build: BuildRecord
    ffmpeg: BinaryRecord
    ffprobe: BinaryRecord


class ResolveBinding(StrictModel):
    report_path: str
    report_sha256: Sha256Value
    version: str
    build: str
    edition: Literal["studio", "free", "unverified"]
    needs_live_verification: bool


class SmokeRecord(StrictModel):
    status: Literal["pending", "passed"]
    evidence_paths: tuple[str, ...]
    observation: str


class SmokeResults(StrictModel):
    resolve_readonly: SmokeRecord
    ffmpeg_probe: SmokeRecord


class ToolchainLock(StrictModel):
    schema_version: Literal["phase-toolchain-lock-v1"] = "phase-toolchain-lock-v1"
    phase: Literal["phase-0a"] = "phase-0a"
    python: PythonToolchain
    ffmpeg: FfmpegToolchain
    resolve: ResolveBinding
    smoke: SmokeResults


def load_lock(path: Path) -> ToolchainLock:
    try:
        raw = path.read_bytes()
        lock = ToolchainLock.model_validate_json(raw)
    except (OSError, ValidationError) as error:
        raise LockError(f"invalid toolchain lock: {error}") from error
    if raw != canonical_model_bytes(lock):
        raise LockError("noncanonical toolchain lock")
    return lock


def write_lock(path: Path, lock: ToolchainLock) -> None:
    atomic_write(path, canonical_model_bytes(lock))


def update_host_binding(path: Path, report_path: Path, report: ResolveHostReport) -> None:
    lock = load_lock(path)
    resolved_report = report_path.resolve(strict=True)
    binding = ResolveBinding(
        report_path=str(resolved_report),
        report_sha256=sha256_file(resolved_report),
        version=report.application.version,
        build=report.application.build,
        edition=report.application.edition.value,
        needs_live_verification=(
            report.application.edition.needs_live_verification
            or report.scripting.needs_live_verification
        ),
    )
    smoke = lock.smoke.model_copy(
        update={
            "resolve_readonly": SmokeRecord(
                status="passed",
                evidence_paths=(str(resolved_report),),
                observation="read-only inventory completed; no network or Resolve process launch",
            )
        }
    )
    write_lock(path, lock.model_copy(update={"resolve": binding, "smoke": smoke}))
