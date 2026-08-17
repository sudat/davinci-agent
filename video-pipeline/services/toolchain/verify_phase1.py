"""Phase-1-technical toolchain verification: provenance + pinned smokes."""

from __future__ import annotations

from pathlib import Path

from services.toolchain.editorial_model import (
    EditorialModelSmokeError,
    run_editorial_model_smoke,
    verify_editorial_model_contract,
)
from services.toolchain.models import (
    LockError,
    Phase1TechnicalToolchainLock,
    SmokeRecord,
)
from services.toolchain.whisper_ja import (
    WhisperSmokeError,
    run_whisper_smoke,
    verify_whisper_provenance,
)

type SmokeName = str


def verify_phase1_provenance(lock: Phase1TechnicalToolchainLock) -> None:
    try:
        verify_whisper_provenance(lock.whisper_ja)
        verify_editorial_model_contract(lock.editorial_model)
    except WhisperSmokeError as error:
        raise LockError(f"whisper-ja provenance rejected: {error}") from error
    except EditorialModelSmokeError as error:
        raise LockError(f"editorial-model contract rejected: {error}") from error


def phase1_smoke_complete(lock: Phase1TechnicalToolchainLock, names: tuple[SmokeName, ...]) -> bool:
    if lock.smoke.whisper_ja.status == "passed" and (
        lock.smoke.editorial_model.status == "passed"
    ):
        return True
    pending = []
    if lock.smoke.whisper_ja.status != "passed":
        pending.append("whisper-ja")
    if lock.smoke.editorial_model.status != "passed":
        pending.append("editorial-model")
    return all(name in names for name in pending)


def run_phase1_smokes(
    lock: Phase1TechnicalToolchainLock,
    smoke_names: tuple[SmokeName, ...],
) -> Phase1TechnicalToolchainLock:
    updates: dict[str, SmokeRecord] = {}
    prefix = Path(lock.ffmpeg.ffmpeg.path).parent.parent
    if "whisper-ja" in smoke_names and lock.smoke.whisper_ja.status != "passed":
        artifacts = run_whisper_smoke(
            lock.ffmpeg.ffmpeg.path,
            lock.whisper_ja,
            prefix.parents[1] / "smoke" / "whisper-ja",
        )
        updates["whisper_ja"] = SmokeRecord(
            status="passed",
            evidence_paths=tuple(str(item.resolve()) for item in artifacts),
            observation=(
                "synthesized Japanese speech with the pinned TTS voice, preprocessed "
                "with the frozen argv, and transcribed it with the pinned whisper-cli "
                "+ model; the fed key phrase was asserted in the real transcript"
            ),
        )
    if "editorial-model" in smoke_names and lock.smoke.editorial_model.status != "passed":
        evidence = run_editorial_model_smoke(
            lock.editorial_model,
            prefix.parents[1] / "smoke" / "editorial-model",
        )
        updates["editorial_model"] = SmokeRecord(
            status="passed",
            evidence_paths=(str(evidence.resolve()),),
            observation=(
                "contract-only offline validation: schema ids resolved against "
                "services.contracts and the canned replay round-tripped byte-stably; "
                "no network, no credentials, live verification deferred to Todo 39"
            ),
        )
    if not updates:
        return lock
    return lock.model_copy(update={"smoke": lock.smoke.model_copy(update=updates)})
