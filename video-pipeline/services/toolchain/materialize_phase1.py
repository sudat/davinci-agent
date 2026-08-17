"""Phase-1-technical toolchain lock materialization (parent: phase-0c)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import atomic_write, canonical_model_bytes
from services.toolchain.editorial_model import EditorialModelSection
from services.toolchain.materialize import MaterializeError
from services.toolchain.models import (
    Phase0CToolchainLock,
    Phase1TechnicalSmokeResults,
    Phase1TechnicalToolchainLock,
    SmokeRecord,
    load_lock,
)
from services.toolchain.whisper_ja import WhisperJaSection


def _load_whisper_pin(path: Path) -> WhisperJaSection:
    try:
        raw = path.read_bytes()
        section = WhisperJaSection.model_validate_json(raw)
    except (OSError, ValidationError) as error:
        raise MaterializeError(f"invalid whisper-ja pin: {error}") from error
    if raw != canonical_model_bytes(section):
        raise MaterializeError("whisper-ja pin is noncanonical")
    return section


def _load_editorial_pin(path: Path) -> EditorialModelSection:
    try:
        raw = path.read_bytes()
        section = EditorialModelSection.model_validate_json(raw)
    except (OSError, ValidationError) as error:
        raise MaterializeError(f"invalid editorial-model pin: {error}") from error
    if raw != canonical_model_bytes(section):
        raise MaterializeError("editorial-model pin is noncanonical")
    return section


def _assert_phase1_inheritance(
    parent: Phase0CToolchainLock,
    child: Phase1TechnicalToolchainLock,
) -> None:
    parent_view = parent.model_dump(mode="json")
    child_view = child.model_dump(mode="json")
    for section in ("python", "ffmpeg", "resolve", "normalization", "preview_review"):
        if child_view[section] != parent_view[section]:
            raise MaterializeError(f"phase-1-technical must inherit the exact 0C {section} section")
    smoke = child_view["smoke"]
    for record in ("resolve_readonly", "ffmpeg_probe", "ffmpeg_normalize", "preview_review"):
        if smoke[record] != parent_view["smoke"][record]:
            raise MaterializeError(
                f"phase-1-technical must inherit the exact 0C {record} smoke record"
            )
    if smoke["whisper_ja"]["status"] != "pending":
        raise MaterializeError("merged whisper-ja smoke must start pending")
    if smoke["editorial_model"]["status"] != "pending":
        raise MaterializeError("merged editorial-model smoke must start pending")
    if child.ffmpeg.ffmpeg.sha256 != parent.ffmpeg.ffmpeg.sha256:
        raise MaterializeError("ffmpeg binary hash substitution is forbidden")
    if child.ffmpeg.ffprobe.sha256 != parent.ffmpeg.ffprobe.sha256:
        raise MaterializeError("ffprobe binary hash substitution is forbidden")
    if child.editorial_model.external_credentials != "none":
        raise MaterializeError("phase-1-technical pins no external credentials")


def materialize_phase1_technical(
    parent_path: Path,
    whisper_pin: Path,
    editorial_pin: Path,
    out_path: Path,
) -> None:
    parent = load_lock(parent_path)
    if not isinstance(parent, Phase0CToolchainLock):
        raise MaterializeError("phase-1-technical parent must be the frozen phase-0c lock")
    parent_smoke_ok = (
        parent.smoke.resolve_readonly.status == "passed"
        and parent.smoke.ffmpeg_probe.status == "passed"
        and parent.smoke.ffmpeg_normalize.status == "passed"
        and parent.smoke.preview_review.status == "passed"
    )
    if not parent_smoke_ok:
        raise MaterializeError("parent phase-0c smoke results are incomplete")
    whisper = _load_whisper_pin(whisper_pin)
    editorial = _load_editorial_pin(editorial_pin)
    child = Phase1TechnicalToolchainLock(
        phase="phase-1-technical",
        python=parent.python,
        ffmpeg=parent.ffmpeg,
        resolve=parent.resolve,
        normalization=parent.normalization,
        preview_review=parent.preview_review,
        whisper_ja=whisper,
        editorial_model=editorial,
        smoke=Phase1TechnicalSmokeResults(
            resolve_readonly=parent.smoke.resolve_readonly,
            ffmpeg_probe=parent.smoke.ffmpeg_probe,
            ffmpeg_normalize=parent.smoke.ffmpeg_normalize,
            preview_review=parent.smoke.preview_review,
            whisper_ja=SmokeRecord(
                status="pending",
                evidence_paths=(),
                observation="pending whisper-ja smoke",
            ),
            editorial_model=SmokeRecord(
                status="pending",
                evidence_paths=(),
                observation="pending editorial-model contract smoke",
            ),
        ),
    )
    _assert_phase1_inheritance(parent, child)
    payload = canonical_model_bytes(child)
    if out_path.exists():
        if out_path.read_bytes() != payload:
            raise MaterializeError("existing phase-1-technical lock differs from merged result")
        return
    atomic_write(out_path, payload)
