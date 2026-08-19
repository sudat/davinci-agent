from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Final, Literal

from pydantic import ValidationError

from services.foundation_io import sha256_file
from services.resolve_bridge.readiness import HostReadinessError, load_host_report
from services.toolchain.editorial_model import EditorialModelSmokeError
from services.toolchain.models import (
    FROZEN_CONFIGURE_ARGV,
    AnyToolchainLock,
    BinaryRecord,
    LockError,
    Phase0BToolchainLock,
    Phase0CToolchainLock,
    Phase1TechnicalToolchainLock,
    Phase2ToolchainLock,
    SmokeRecord,
    ToolchainLock,
    load_lock,
    write_lock,
)
from services.toolchain.normalization import run_normalize_smoke
from services.toolchain.preview_review import run_preview_smoke
from services.toolchain.render_qc import RenderQcSmokeError
from services.toolchain.resolve_package import ResolvePackageSmokeError
from services.toolchain.smoke import ProbeAssertionError
from services.toolchain.verify_phase0a import verify_phase0a_smoke
from services.toolchain.verify_phase1 import (
    phase1_smoke_complete,
    run_phase1_smokes,
    verify_phase1_provenance,
)
from services.toolchain.verify_phase2 import (
    phase2_smoke_complete,
    run_phase2_smokes,
    verify_phase2_provenance,
)
from services.toolchain.whisper_ja import WhisperSmokeError

type SmokeName = Literal[
    "resolve-readonly",
    "ffmpeg-probe",
    "ffmpeg-normalize",
    "preview-review",
    "whisper-ja",
    "editorial-model",
    "resolve-package",
    "render-qc",
]


def verify_binary(record: BinaryRecord) -> None:
    path = Path(record.path)
    if not path.is_file() or sha256_file(path) != record.sha256:
        raise LockError(f"binary hash drift: {path}")


def _verify_ffmpeg_provenance(lock: AnyToolchainLock) -> None:
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


def _verify_resolve_provenance(lock: AnyToolchainLock) -> None:
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


def _verify_provenance(lock: AnyToolchainLock) -> None:
    _verify_ffmpeg_provenance(lock)
    _verify_resolve_provenance(lock)
    if isinstance(lock, Phase0BToolchainLock | Phase0CToolchainLock):
        verify_binary(lock.ffmpeg.ffmpeg)
        verify_binary(lock.ffmpeg.ffprobe)
    if isinstance(lock, Phase1TechnicalToolchainLock):
        verify_phase1_provenance(lock)
    if isinstance(lock, Phase2ToolchainLock):
        verify_phase2_provenance(lock, Path.cwd().resolve())


def _run_normalize_smoke(lock: Phase0BToolchainLock) -> SmokeRecord:
    prefix = Path(lock.ffmpeg.ffmpeg.path).parent.parent
    artifacts = run_normalize_smoke(
        lock.ffmpeg.ffmpeg.path,
        lock.ffmpeg.ffprobe.path,
        lock.normalization,
        prefix.parents[1] / "smoke" / "normalize",
    )
    return SmokeRecord(
        status="passed",
        evidence_paths=tuple(str(item.resolve()) for item in artifacts),
        observation=(
            "generated testsrc2 input, applied the locked normalization recipe, and "
            "probed the CFR30 roundtrip expectation"
        ),
    )


def _run_preview_smoke(lock: Phase0CToolchainLock) -> SmokeRecord:
    prefix = Path(lock.ffmpeg.ffmpeg.path).parent.parent
    artifacts = run_preview_smoke(
        lock.ffmpeg.ffmpeg.path,
        lock.ffmpeg.ffprobe.path,
        lock.preview_review,
        prefix.parents[1] / "smoke" / "preview-review",
    )
    return SmokeRecord(
        status="passed",
        evidence_paths=tuple(str(item.resolve()) for item in artifacts),
        observation=(
            "encoded and probed the pinned-ffmpeg preview profile; no external "
            "model participated"
        ),
    )


def _phase0c_smoke_complete(lock: Phase0CToolchainLock, names: tuple[SmokeName, ...]) -> bool:
    if lock.smoke.preview_review.status == "passed":
        return True
    return "preview-review" in names


def _apply_smokes(
    lock: AnyToolchainLock, smoke_names: tuple[SmokeName, ...]
) -> AnyToolchainLock:
    match lock:
        case Phase0BToolchainLock() as phase0b:
            updates: dict[str, SmokeRecord] = {}
            if "ffmpeg-normalize" in smoke_names:
                updates["ffmpeg_normalize"] = _run_normalize_smoke(phase0b)
            return lock.model_copy(update={"smoke": phase0b.smoke.model_copy(update=updates)})
        case Phase0CToolchainLock() as phase0c:
            phase0c_updates: dict[str, SmokeRecord] = {}
            if "preview-review" in smoke_names and phase0c.smoke.preview_review.status != "passed":
                phase0c_updates["preview_review"] = _run_preview_smoke(phase0c)
            return lock.model_copy(
                update={"smoke": phase0c.smoke.model_copy(update=phase0c_updates)}
            )
        case Phase1TechnicalToolchainLock():
            return run_phase1_smokes(lock, smoke_names)
        case Phase2ToolchainLock():
            return run_phase2_smokes(lock, smoke_names)
        case ToolchainLock():
            return verify_phase0a_smoke(lock, smoke_names)


def verify_lock(path: Path, smoke_names: tuple[SmokeName, ...]) -> AnyToolchainLock:
    lock = load_lock(path)
    _verify_provenance(lock)
    if isinstance(lock, Phase0BToolchainLock) and not _phase0b_smoke_complete(
        lock, smoke_names
    ):
        raise LockError("incomplete Phase 0B toolchain smoke results")
    if isinstance(lock, Phase0CToolchainLock) and not _phase0c_smoke_complete(
        lock, smoke_names
    ):
        raise LockError("incomplete Phase 0C toolchain smoke results")
    if isinstance(lock, Phase1TechnicalToolchainLock) and not phase1_smoke_complete(
        lock, smoke_names
    ):
        raise LockError("incomplete Phase 1 toolchain smoke results")
    if isinstance(lock, Phase2ToolchainLock) and not phase2_smoke_complete(
        lock, smoke_names
    ):
        raise LockError("incomplete Phase 2 toolchain smoke results")
    verified = _apply_smokes(lock, smoke_names)
    write_lock(path, verified)
    return verified


KNOWN_SMOKES: Final[frozenset[str]] = frozenset(
    {
        "resolve-readonly",
        "ffmpeg-probe",
        "ffmpeg-normalize",
        "preview-review",
        "whisper-ja",
        "editorial-model",
        "resolve-package",
        "render-qc",
    }
)
SMOKE_ALIASES: Final[dict[str, str]] = {"ffmpeg-ffprobe": "ffmpeg-probe"}


def _phase0b_smoke_complete(lock: Phase0BToolchainLock, names: tuple[SmokeName, ...]) -> bool:
    if lock.smoke.ffmpeg_normalize.status == "passed":
        return True
    return "ffmpeg-normalize" in names


def _smoke_names(raw: str) -> tuple[SmokeName, ...]:
    names = tuple(raw.split(","))
    if not names:
        raise LockError(f"unsupported smoke profile: {raw}")
    parsed: list[SmokeName] = []
    for name in names:
        canonical = SMOKE_ALIASES.get(name, name)
        if canonical not in KNOWN_SMOKES:
            raise LockError(f"unsupported smoke profile: {raw}")
        parsed.append(canonical)  # type: ignore[arg-type]
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
        RenderQcSmokeError,
        ResolvePackageSmokeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        ValidationError,
        WhisperSmokeError,
        EditorialModelSmokeError,
    ) as error:
        print(error)
        return 2
    print(f"toolchain verified: {','.join(smoke_names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
