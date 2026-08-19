"""Phase-2 toolchain lock materialization (parent: phase-1-technical)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import atomic_write, canonical_model_bytes
from services.toolchain.materialize import MaterializeError
from services.toolchain.models import (
    Phase1TechnicalToolchainLock,
    Phase2SmokeResults,
    Phase2ToolchainLock,
    SmokeRecord,
    load_lock,
)
from services.toolchain.render_qc import RenderQcSection
from services.toolchain.resolve_package import ResolvePackageSection


def _load_resolve_package_pin(path: Path) -> ResolvePackageSection:
    try:
        raw = path.read_bytes()
        section = ResolvePackageSection.model_validate_json(raw)
    except (OSError, ValidationError) as error:
        raise MaterializeError(f"invalid resolve-package pin: {error}") from error
    if raw != canonical_model_bytes(section):
        raise MaterializeError("resolve-package pin is noncanonical")
    return section


def _load_render_qc_pin(path: Path) -> RenderQcSection:
    try:
        raw = path.read_bytes()
        section = RenderQcSection.model_validate_json(raw)
    except (OSError, ValidationError) as error:
        raise MaterializeError(f"invalid render-qc pin: {error}") from error
    if raw != canonical_model_bytes(section):
        raise MaterializeError("render-qc pin is noncanonical")
    return section


def _assert_phase2_inheritance(
    parent: Phase1TechnicalToolchainLock,
    child: Phase2ToolchainLock,
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
    ):
        if child_view[section] != parent_view[section]:
            raise MaterializeError(f"phase-2 must inherit the exact 1T {section} section")
    smoke = child_view["smoke"]
    for record in (
        "resolve_readonly",
        "ffmpeg_probe",
        "ffmpeg_normalize",
        "preview_review",
        "whisper_ja",
        "editorial_model",
    ):
        if smoke[record] != parent_view["smoke"][record]:
            raise MaterializeError(f"phase-2 must inherit the exact 1T {record} smoke record")
    if smoke["resolve_package"]["status"] != "pending":
        raise MaterializeError("merged resolve-package smoke must start pending")
    if smoke["render_qc"]["status"] != "pending":
        raise MaterializeError("merged render-qc smoke must start pending")
    if child.ffmpeg.ffmpeg.sha256 != parent.ffmpeg.ffmpeg.sha256:
        raise MaterializeError("ffmpeg binary hash substitution is forbidden")
    if child.ffmpeg.ffprobe.sha256 != parent.ffmpeg.ffprobe.sha256:
        raise MaterializeError("ffprobe binary hash substitution is forbidden")
    if child.editorial_model.external_credentials != "none":
        raise MaterializeError("phase-2 pins no external credentials")


def materialize_phase2(
    parent_path: Path,
    resolve_package_pin: Path,
    render_qc_pin: Path,
    out_path: Path,
) -> None:
    parent = load_lock(parent_path)
    if not isinstance(parent, Phase1TechnicalToolchainLock):
        raise MaterializeError("phase-2 parent must be the frozen phase-1-technical lock")
    parent_smoke_ok = (
        parent.smoke.resolve_readonly.status == "passed"
        and parent.smoke.ffmpeg_probe.status == "passed"
        and parent.smoke.ffmpeg_normalize.status == "passed"
        and parent.smoke.preview_review.status == "passed"
        and parent.smoke.whisper_ja.status == "passed"
        and parent.smoke.editorial_model.status == "passed"
    )
    if not parent_smoke_ok:
        raise MaterializeError("parent phase-1-technical smoke results are incomplete")
    resolve_package = _load_resolve_package_pin(resolve_package_pin)
    render_qc = _load_render_qc_pin(render_qc_pin)
    child = Phase2ToolchainLock(
        phase="phase-2",
        python=parent.python,
        ffmpeg=parent.ffmpeg,
        resolve=parent.resolve,
        normalization=parent.normalization,
        preview_review=parent.preview_review,
        whisper_ja=parent.whisper_ja,
        editorial_model=parent.editorial_model,
        resolve_package=resolve_package,
        render_qc=render_qc,
        smoke=Phase2SmokeResults(
            resolve_readonly=parent.smoke.resolve_readonly,
            ffmpeg_probe=parent.smoke.ffmpeg_probe,
            ffmpeg_normalize=parent.smoke.ffmpeg_normalize,
            preview_review=parent.smoke.preview_review,
            whisper_ja=parent.smoke.whisper_ja,
            editorial_model=parent.smoke.editorial_model,
            resolve_package=SmokeRecord(
                status="pending",
                evidence_paths=(),
                observation="pending resolve-package capability-matrix smoke",
            ),
            render_qc=SmokeRecord(
                status="pending",
                evidence_paths=(),
                observation="pending render-qc completion-contract smoke",
            ),
        ),
    )
    _assert_phase2_inheritance(parent, child)
    payload = canonical_model_bytes(child)
    if out_path.exists():
        if out_path.read_bytes() != payload:
            raise MaterializeError("existing phase-2 lock differs from merged result")
        return
    atomic_write(out_path, payload)
