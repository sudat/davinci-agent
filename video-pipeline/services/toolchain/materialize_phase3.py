"""Phase-3 toolchain lock materialization (parent: phase-2)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import atomic_write, canonical_model_bytes
from services.toolchain.materialize import MaterializeError
from services.toolchain.models import (
    Phase2ToolchainLock,
    Phase3SmokeResults,
    Phase3ToolchainLock,
    SmokeRecord,
    load_lock,
)
from services.toolchain.presentation_assets import PresentationAssetsSection


def _load_presentation_assets_pin(path: Path) -> PresentationAssetsSection:
    try:
        raw = path.read_bytes()
        section = PresentationAssetsSection.model_validate_json(raw)
    except (OSError, ValidationError) as error:
        raise MaterializeError(f"invalid presentation-assets pin: {error}") from error
    if raw != canonical_model_bytes(section):
        raise MaterializeError("presentation-assets pin is noncanonical")
    return section


def _assert_phase3_inheritance(
    parent: Phase2ToolchainLock,
    child: Phase3ToolchainLock,
) -> None:
    parent_view = parent.model_dump(mode="json")
    child_view = child.model_dump(mode="json")
    for section in (
        "python",
        "ffmpeg",
        "resolve",
        "normalization",
        "preview_review",
        "whisper_ja",
        "editorial_model",
        "resolve_package",
        "render_qc",
    ):
        if child_view[section] != parent_view[section]:
            raise MaterializeError(f"phase-3 must inherit the exact 2 {section} section")
    smoke = child_view["smoke"]
    for record in (
        "resolve_readonly",
        "ffmpeg_probe",
        "ffmpeg_normalize",
        "preview_review",
        "whisper_ja",
        "editorial_model",
        "resolve_package",
        "render_qc",
    ):
        if smoke[record] != parent_view["smoke"][record]:
            raise MaterializeError(f"phase-3 must inherit the exact 2 {record} smoke record")
    if smoke["presentation_assets"]["status"] != "pending":
        raise MaterializeError("merged presentation-assets smoke must start pending")
    _assert_no_phase3_substitution(parent, child)


def _assert_no_phase3_substitution(parent: Phase2ToolchainLock, child: Phase3ToolchainLock) -> None:
    if child.ffmpeg.ffmpeg.sha256 != parent.ffmpeg.ffmpeg.sha256:
        raise MaterializeError("ffmpeg binary hash substitution is forbidden")
    if child.ffmpeg.ffprobe.sha256 != parent.ffmpeg.ffprobe.sha256:
        raise MaterializeError("ffprobe binary hash substitution is forbidden")
    if child.presentation_assets.external_credentials != "none":
        raise MaterializeError("phase-3 pins no external credentials")
    if child.presentation_assets.third_party_material != "none":
        raise MaterializeError("phase-3 pins no third-party material")
    if child.presentation_assets.production_brand_claimed is not False:
        raise MaterializeError("phase-3 pins no production-brand claim")


def materialize_phase3(
    parent_path: Path,
    presentation_assets_pin: Path,
    out_path: Path,
) -> None:
    parent = load_lock(parent_path)
    if not isinstance(parent, Phase2ToolchainLock):
        raise MaterializeError("phase-3 parent must be the frozen phase-2 lock")
    parent_smoke_ok = (
        parent.smoke.resolve_readonly.status == "passed"
        and parent.smoke.ffmpeg_probe.status == "passed"
        and parent.smoke.ffmpeg_normalize.status == "passed"
        and parent.smoke.preview_review.status == "passed"
        and parent.smoke.whisper_ja.status == "passed"
        and parent.smoke.editorial_model.status == "passed"
        and parent.smoke.resolve_package.status == "passed"
        and parent.smoke.render_qc.status == "passed"
    )
    if not parent_smoke_ok:
        raise MaterializeError("parent phase-2 smoke results are incomplete")
    presentation_assets = _load_presentation_assets_pin(presentation_assets_pin)
    child = Phase3ToolchainLock(
        phase="phase-3",
        python=parent.python,
        ffmpeg=parent.ffmpeg,
        resolve=parent.resolve,
        normalization=parent.normalization,
        preview_review=parent.preview_review,
        whisper_ja=parent.whisper_ja,
        editorial_model=parent.editorial_model,
        resolve_package=parent.resolve_package,
        render_qc=parent.render_qc,
        presentation_assets=presentation_assets,
        smoke=Phase3SmokeResults(
            resolve_readonly=parent.smoke.resolve_readonly,
            ffmpeg_probe=parent.smoke.ffmpeg_probe,
            ffmpeg_normalize=parent.smoke.ffmpeg_normalize,
            preview_review=parent.smoke.preview_review,
            whisper_ja=parent.smoke.whisper_ja,
            editorial_model=parent.smoke.editorial_model,
            resolve_package=parent.smoke.resolve_package,
            render_qc=parent.smoke.render_qc,
            presentation_assets=SmokeRecord(
                status="pending",
                evidence_paths=(),
                observation="pending presentation-assets rights smoke",
            ),
        ),
    )
    _assert_phase3_inheritance(parent, child)
    payload = canonical_model_bytes(child)
    if out_path.exists():
        if out_path.read_bytes() != payload:
            raise MaterializeError("existing phase-3 lock differs from merged result")
        return
    atomic_write(out_path, payload)
