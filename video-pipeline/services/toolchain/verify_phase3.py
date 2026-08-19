"""Phase-3 toolchain verification: provenance + pinned smokes."""

from __future__ import annotations

from pathlib import Path

from services.toolchain.models import (
    LockError,
    Phase2SmokeResults,
    Phase2ToolchainLock,
    Phase3ToolchainLock,
    SmokeRecord,
)
from services.toolchain.presentation_assets import (
    PresentationAssetsSmokeError,
    run_presentation_assets_smoke,
    verify_presentation_assets_contract,
)
from services.toolchain.verify_phase2 import verify_phase2_provenance

type SmokeName = str


def verify_phase3_provenance(lock: Phase3ToolchainLock, root: Path) -> None:
    phase2_view = Phase2ToolchainLock(
        phase="phase-2",
        python=lock.python,
        ffmpeg=lock.ffmpeg,
        resolve=lock.resolve,
        normalization=lock.normalization,
        preview_review=lock.preview_review,
        whisper_ja=lock.whisper_ja,
        editorial_model=lock.editorial_model,
        resolve_package=lock.resolve_package,
        render_qc=lock.render_qc,
        smoke=Phase2SmokeResults(
            resolve_readonly=lock.smoke.resolve_readonly,
            ffmpeg_probe=lock.smoke.ffmpeg_probe,
            ffmpeg_normalize=lock.smoke.ffmpeg_normalize,
            preview_review=lock.smoke.preview_review,
            whisper_ja=lock.smoke.whisper_ja,
            editorial_model=lock.smoke.editorial_model,
            resolve_package=lock.smoke.resolve_package,
            render_qc=lock.smoke.render_qc,
        ),
    )
    verify_phase2_provenance(phase2_view, root)
    try:
        verify_presentation_assets_contract(lock.presentation_assets, root)
    except PresentationAssetsSmokeError as error:
        raise LockError(f"presentation-assets contract rejected: {error}") from error


def phase3_smoke_complete(lock: Phase3ToolchainLock, names: tuple[SmokeName, ...]) -> bool:
    if lock.smoke.presentation_assets.status == "passed":
        return True
    return "presentation-assets" in names


def run_phase3_smokes(
    lock: Phase3ToolchainLock,
    smoke_names: tuple[SmokeName, ...],
) -> Phase3ToolchainLock:
    updates: dict[str, SmokeRecord] = {}
    prefix = Path(lock.ffmpeg.ffmpeg.path).parent.parent
    smoke_root = prefix.parents[1] / "smoke"
    if "presentation-assets" in smoke_names and (lock.smoke.presentation_assets.status != "passed"):
        evidence = run_presentation_assets_smoke(
            lock.presentation_assets,
            Path.cwd().resolve(),
            smoke_root / "presentation-assets",
        )
        updates["presentation_assets"] = SmokeRecord(
            status="passed",
            evidence_paths=(str(evidence.resolve()),),
            observation=(
                "contract-only offline validation: every stored brand asset byte "
                "hashes to the manifest-declared value, every recipe is lavfi/"
                "aevalsrc-only, and all rights are owner-created with no "
                "third-party or production-brand claim; no network, no Resolve"
            ),
        )
    if not updates:
        return lock
    return lock.model_copy(update={"smoke": lock.smoke.model_copy(update=updates)})
