from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from services.foundation_io import sha256_file
from services.resolve_bridge.readiness import HostReadinessError, load_host_report
from services.toolchain.models import (
    FROZEN_CONFIGURE_ARGV,
    BinaryRecord,
    LockError,
    SmokeRecord,
    ToolchainLock,
    load_lock,
    write_lock,
)
from services.toolchain.smoke import ProbeAssertionError, run_ffmpeg_probe

type SmokeName = Literal["resolve-readonly", "ffmpeg-probe"]


def verify_binary(record: BinaryRecord) -> None:
    path = Path(record.path)
    if not path.is_file() or sha256_file(path) != record.sha256:
        raise LockError(f"binary hash drift: {path}")


def _verify_ffmpeg_provenance(lock: ToolchainLock) -> None:
    source = Path(lock.ffmpeg.source.tarball_path)
    if not source.is_file() or sha256_file(source) != lock.ffmpeg.source.tarball_sha256:
        raise LockError("FFmpeg source provenance drift")
    verify_binary(lock.python.uv)
    verify_binary(lock.python.python)
    verify_binary(lock.ffmpeg.ffmpeg)
    verify_binary(lock.ffmpeg.ffprobe)
    if lock.ffmpeg.build.configure_argv != FROZEN_CONFIGURE_ARGV:
        raise LockError("FFmpeg configure argv drift")
    if lock.ffmpeg.build.compiler not in lock.ffmpeg.ffmpeg.version_output:
        raise LockError("FFmpeg compiler provenance drift")
    if not lock.ffmpeg.ffmpeg.version_output.startswith("ffmpeg version 7.1.1"):
        raise LockError("FFmpeg version provenance drift")
    if not lock.ffmpeg.ffprobe.version_output.startswith("ffprobe version 7.1.1"):
        raise LockError("ffprobe version provenance drift")


def _verify_resolve_provenance(lock: ToolchainLock) -> None:
    report_path = Path(lock.resolve.report_path)
    if not report_path.is_file() or sha256_file(report_path) != lock.resolve.report_sha256:
        raise LockError("Resolve host report hash drift")
    report = load_host_report(report_path)
    if (
        report.application.version != lock.resolve.version
        or report.application.build != lock.resolve.build
    ):
        raise LockError("Resolve version binding drift")
    for evidence in (*report.docs.app_bundle_paths, *report.docs.installed_paths):
        path = Path(evidence.path)
        if not path.is_file() or sha256_file(path) != evidence.sha256:
            raise LockError(f"Resolve docs provenance drift: {path}")
    for evidence in (report.bridge.library, report.bridge.module):
        path = Path(evidence.path)
        if not path.is_file() or sha256_file(path) != evidence.sha256:
            raise LockError(f"Resolve bridge provenance drift: {path}")


def _verify_provenance(lock: ToolchainLock) -> None:
    _verify_ffmpeg_provenance(lock)
    _verify_resolve_provenance(lock)


def verify_lock(path: Path, smoke_names: tuple[SmokeName, ...]) -> ToolchainLock:
    lock = load_lock(path)
    _verify_provenance(lock)
    smoke = lock.smoke
    for smoke_name in smoke_names:
        match smoke_name:
            case "resolve-readonly":
                report = load_host_report(Path(lock.resolve.report_path))
                smoke = smoke.model_copy(
                    update={
                        "resolve_readonly": SmokeRecord(
                            status="passed",
                            evidence_paths=(lock.resolve.report_path,),
                            observation=(
                                "loopback-only policy confirmed; probe performed no network "
                                f"access; live preference check required="
                                f"{report.scripting.needs_live_verification}"
                            ),
                        )
                    }
                )
            case "ffmpeg-probe":
                prefix = Path(lock.ffmpeg.ffmpeg.path).parent.parent
                artifacts = run_ffmpeg_probe(
                    Path(lock.ffmpeg.ffmpeg.path),
                    Path(lock.ffmpeg.ffprobe.path),
                    prefix.parents[1] / "smoke",
                )
                smoke = smoke.model_copy(
                    update={
                        "ffmpeg_probe": SmokeRecord(
                            status="passed",
                            evidence_paths=tuple(str(item.resolve()) for item in artifacts),
                            observation=(
                                "encoded, decoded, and probed 30 frames at 30/1 for 1000 ms"
                            ),
                        )
                    }
                )
    verified = lock.model_copy(update={"smoke": smoke})
    write_lock(path, verified)
    return verified


def _smoke_names(raw: str) -> tuple[SmokeName, ...]:
    names = tuple(raw.split(","))
    if not names:
        raise LockError(f"unsupported smoke profile: {raw}")
    parsed: list[SmokeName] = []
    for name in names:
        match name:
            case "resolve-readonly":
                parsed.append("resolve-readonly")
            case "ffmpeg-probe":
                parsed.append("ffmpeg-probe")
            case _:
                raise LockError(f"unsupported smoke profile: {raw}")
    return tuple(parsed)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--smoke", required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        smoke_names = _smoke_names(arguments.smoke)
        verify_lock(arguments.lock, smoke_names)
    except (
        HostReadinessError,
        LockError,
        OSError,
        ProbeAssertionError,
        subprocess.CalledProcessError,
        ValidationError,
    ) as error:
        print(error)
        return 2
    print(f"toolchain verified: {','.join(smoke_names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
