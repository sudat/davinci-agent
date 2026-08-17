from __future__ import annotations

import argparse
from pathlib import Path

from pydantic import ValidationError

from services.foundation_io import atomic_write, canonical_model_bytes
from services.toolchain.models import (
    LockError,
    Phase0BSmokeResults,
    Phase0BToolchainLock,
    Phase0CSmokeResults,
    Phase0CToolchainLock,
    SmokeRecord,
    ToolchainLock,
    load_lock,
)
from services.toolchain.normalization import NormalizationSection
from services.toolchain.preview_review import PreviewReviewSection

PINS_ROOT = Path("config/toolchains/pins")


class MaterializeError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def __str__(self) -> str:
        return self.detail


def _load_pin(path: Path) -> NormalizationSection:
    try:
        raw = path.read_bytes()
        section = NormalizationSection.model_validate_json(raw)
    except (OSError, ValidationError) as error:
        raise MaterializeError(f"invalid normalization pin: {error}") from error
    if raw != canonical_model_bytes(section):
        raise MaterializeError("normalization pin is noncanonical")
    return section


def _load_preview_pin(path: Path) -> PreviewReviewSection:
    try:
        raw = path.read_bytes()
        section = PreviewReviewSection.model_validate_json(raw)
    except (OSError, ValidationError) as error:
        raise MaterializeError(f"invalid preview-review pin: {error}") from error
    if raw != canonical_model_bytes(section):
        raise MaterializeError("preview-review pin is noncanonical")
    return section


def materialize_phase0b(parent_path: Path, pin_path: Path, out_path: Path) -> None:
    parent = load_lock(parent_path)
    if not isinstance(parent, ToolchainLock):
        raise MaterializeError("phase-0b parent must be the frozen phase-0a lock")
    parent_smoke_ok = (
        parent.smoke.resolve_readonly.status == "passed"
        and parent.smoke.ffmpeg_probe.status == "passed"
    )
    if not parent_smoke_ok:
        raise MaterializeError("parent phase-0a smoke results are incomplete")
    section = _load_pin(pin_path)
    inherited_smoke = Phase0BSmokeResults(
        resolve_readonly=parent.smoke.resolve_readonly,
        ffmpeg_probe=parent.smoke.ffmpeg_probe,
        ffmpeg_normalize=SmokeRecord(
            status="pending",
            evidence_paths=(),
            observation="pending ffmpeg-normalize smoke",
        ),
    )
    child = Phase0BToolchainLock(
        phase="phase-0b",
        python=parent.python,
        ffmpeg=parent.ffmpeg,
        resolve=parent.resolve,
        normalization=section,
        smoke=inherited_smoke,
    )
    _assert_inheritance(parent, child)
    payload = canonical_model_bytes(child)
    if out_path.exists():
        if out_path.read_bytes() != payload:
            raise MaterializeError("existing phase-0b lock differs from merged result")
        return
    atomic_write(out_path, payload)


def _assert_inheritance(parent: ToolchainLock, child: Phase0BToolchainLock) -> None:
    parent_view = parent.model_dump(mode="json")
    child_view = child.model_dump(mode="json")
    for section in ("python", "ffmpeg", "resolve"):
        if child_view[section] != parent_view[section]:
            raise MaterializeError(f"phase-0b must inherit the exact 0A {section} section")
    smoke = child_view["smoke"]
    if (
        smoke["resolve_readonly"] != parent_view["smoke"]["resolve_readonly"]
        or smoke["ffmpeg_probe"] != parent_view["smoke"]["ffmpeg_probe"]
    ):
        raise MaterializeError("phase-0b must inherit the exact 0A smoke records")
    if smoke["ffmpeg_normalize"]["status"] != "pending":
        raise MaterializeError("merged ffmpeg-normalize smoke must start pending")
    if child.ffmpeg.ffmpeg.sha256 != parent.ffmpeg.ffmpeg.sha256:
        raise MaterializeError("ffmpeg binary hash substitution is forbidden")
    if child.ffmpeg.ffprobe.sha256 != parent.ffmpeg.ffprobe.sha256:
        raise MaterializeError("ffprobe binary hash substitution is forbidden")


def materialize_phase0c(parent_path: Path, pin_path: Path, out_path: Path) -> None:
    parent = load_lock(parent_path)
    if not isinstance(parent, Phase0BToolchainLock):
        raise MaterializeError("phase-0c parent must be the frozen phase-0b lock")
    parent_smoke_ok = (
        parent.smoke.resolve_readonly.status == "passed"
        and parent.smoke.ffmpeg_probe.status == "passed"
        and parent.smoke.ffmpeg_normalize.status == "passed"
    )
    if not parent_smoke_ok:
        raise MaterializeError("parent phase-0b smoke results are incomplete")
    section = _load_preview_pin(pin_path)
    inherited_smoke = Phase0CSmokeResults(
        resolve_readonly=parent.smoke.resolve_readonly,
        ffmpeg_probe=parent.smoke.ffmpeg_probe,
        ffmpeg_normalize=parent.smoke.ffmpeg_normalize,
        preview_review=SmokeRecord(
            status="pending",
            evidence_paths=(),
            observation="pending preview-review smoke",
        ),
    )
    child = Phase0CToolchainLock(
        phase="phase-0c",
        python=parent.python,
        ffmpeg=parent.ffmpeg,
        resolve=parent.resolve,
        normalization=parent.normalization,
        preview_review=section,
        smoke=inherited_smoke,
    )
    _assert_phase0c_inheritance(parent, child)
    payload = canonical_model_bytes(child)
    if out_path.exists():
        if out_path.read_bytes() != payload:
            raise MaterializeError("existing phase-0c lock differs from merged result")
        return
    atomic_write(out_path, payload)


def _assert_phase0c_inheritance(
    parent: Phase0BToolchainLock,
    child: Phase0CToolchainLock,
) -> None:
    parent_view = parent.model_dump(mode="json")
    child_view = child.model_dump(mode="json")
    for section in ("python", "ffmpeg", "resolve", "normalization"):
        if child_view[section] != parent_view[section]:
            raise MaterializeError(f"phase-0c must inherit the exact 0B {section} section")
    smoke = child_view["smoke"]
    for record in ("resolve_readonly", "ffmpeg_probe", "ffmpeg_normalize"):
        if smoke[record] != parent_view["smoke"][record]:
            raise MaterializeError(f"phase-0c must inherit the exact 0B {record} smoke record")
    if smoke["preview_review"]["status"] != "pending":
        raise MaterializeError("merged preview-review smoke must start pending")
    if child.ffmpeg.ffmpeg.sha256 != parent.ffmpeg.ffmpeg.sha256:
        raise MaterializeError("ffmpeg binary hash substitution is forbidden")
    if child.ffmpeg.ffprobe.sha256 != parent.ffmpeg.ffprobe.sha256:
        raise MaterializeError("ffprobe binary hash substitution is forbidden")
    if child.preview_review.external_model != "none":
        raise MaterializeError("phase-0c pins no external model")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument(
        "--phase", choices=("phase-0b", "phase-0c", "phase-1-technical"), required=True
    )
    parser.add_argument("--pin", required=True)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def resolve_pin_path(raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute() or candidate.suffix == ".json":
        return candidate
    return Path.cwd() / PINS_ROOT / f"{raw}.json"


def resolve_pin_paths(raw: str) -> tuple[Path, Path]:
    names = raw.split(",")
    if names != ["whisper-ja", "editorial-model"]:
        raise MaterializeError("phase-1-technical pins exactly whisper-ja,editorial-model")
    return (
        Path.cwd() / PINS_ROOT / "whisper-ja.json",
        Path.cwd() / PINS_ROOT / "editorial-model.json",
    )


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if arguments.phase == "phase-0b":
            materialize_phase0b(arguments.parent, resolve_pin_path(arguments.pin), arguments.out)
        elif arguments.phase == "phase-0c":
            materialize_phase0c(arguments.parent, resolve_pin_path(arguments.pin), arguments.out)
        else:
            from services.toolchain.materialize_phase1 import (  # noqa: PLC0415 (module cycle)
                materialize_phase1_technical,
            )

            whisper_pin, editorial_pin = resolve_pin_paths(arguments.pin)
            materialize_phase1_technical(
                arguments.parent, whisper_pin, editorial_pin, arguments.out
            )
    except (LockError, MaterializeError, OSError) as error:
        print(error)
        return 2
    print(f"toolchain materialized: {arguments.out} ({arguments.phase})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
