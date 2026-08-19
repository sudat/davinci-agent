"""Phase-2 toolchain verification: provenance + pinned smokes."""

from __future__ import annotations

from pathlib import Path

from services.toolchain.editorial_model import (
    EditorialModelSmokeError,
    verify_editorial_model_contract,
)
from services.toolchain.models import (
    LockError,
    Phase2ToolchainLock,
    SmokeRecord,
)
from services.toolchain.render_qc import (
    RenderQcSmokeError,
    run_render_qc_smoke,
    verify_render_qc_contract,
)
from services.toolchain.resolve_package import (
    ResolvePackageSmokeError,
    run_resolve_package_smoke,
    verify_resolve_package_contract,
)
from services.toolchain.whisper_ja import WhisperSmokeError, verify_whisper_provenance

type SmokeName = str


def verify_phase2_provenance(lock: Phase2ToolchainLock, root: Path) -> None:
    try:
        verify_whisper_provenance(lock.whisper_ja)
        verify_editorial_model_contract(lock.editorial_model)
        verify_resolve_package_contract(lock.resolve_package, root)
        verify_render_qc_contract(lock.render_qc)
    except WhisperSmokeError as error:
        raise LockError(f"whisper-ja provenance rejected: {error}") from error
    except EditorialModelSmokeError as error:
        raise LockError(f"editorial-model contract rejected: {error}") from error
    except ResolvePackageSmokeError as error:
        raise LockError(f"resolve-package contract rejected: {error}") from error
    except RenderQcSmokeError as error:
        raise LockError(f"render-qc contract rejected: {error}") from error


def phase2_smoke_complete(lock: Phase2ToolchainLock, names: tuple[SmokeName, ...]) -> bool:
    if lock.smoke.resolve_package.status == "passed" and lock.smoke.render_qc.status == "passed":
        return True
    pending = []
    if lock.smoke.resolve_package.status != "passed":
        pending.append("resolve-package")
    if lock.smoke.render_qc.status != "passed":
        pending.append("render-qc")
    return all(name in names for name in pending)


def run_phase2_smokes(
    lock: Phase2ToolchainLock,
    smoke_names: tuple[SmokeName, ...],
) -> Phase2ToolchainLock:
    updates: dict[str, SmokeRecord] = {}
    prefix = Path(lock.ffmpeg.ffmpeg.path).parent.parent
    smoke_root = prefix.parents[1] / "smoke"
    if "resolve-package" in smoke_names and lock.smoke.resolve_package.status != "passed":
        evidence = run_resolve_package_smoke(
            lock.resolve_package,
            Path.cwd().resolve(),
            smoke_root / "resolve-package",
        )
        updates["resolve_package"] = SmokeRecord(
            status="passed",
            evidence_paths=(str(evidence.resolve()),),
            observation=(
                "contract-only offline validation: the pinned capability matrix "
                "hashes to the locked value and every required capability is "
                "live-verified with evidence; no Resolve launch, no network; "
                "live clean-build verification deferred to Todo 48"
            ),
        )
    if "render-qc" in smoke_names and lock.smoke.render_qc.status != "passed":
        evidence = run_render_qc_smoke(lock.render_qc, smoke_root / "render-qc")
        updates["render_qc"] = SmokeRecord(
            status="passed",
            evidence_paths=(str(evidence.resolve()),),
            observation=(
                "contract-only offline validation: CompletionPercentage==100 stays "
                "the only machine-readable completion signal (canned localized "
                "payloads classified, including a false complete at 99) and the "
                "fixed preset is pinned; no live render in this smoke"
            ),
        )
    if not updates:
        return lock
    return lock.model_copy(update={"smoke": lock.smoke.model_copy(update=updates)})
