"""Episode-0 longitudinal baseline tooling.

Real-01 is the longitudinal product benchmark: this module provides
(a) deterministic source-manifest freeze (sha256/size/ffprobe duration),
(b) the operator measurement log schema (``episode0-baseline-v1``), and
(c) report generation + comparison that yield stable canonical bytes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BeforeValidator, Field, ValidationError

from services.contracts.primitives import Sha256, StrictModel, to_tuple
from services.foundation_io import atomic_write, canonical_model_bytes, sha256_file

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _coerce_float(value: object) -> object:
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]
NonNegativeFloat = Annotated[
    float,
    BeforeValidator(_coerce_float),
    Field(ge=0, strict=True),
]
Seq = Annotated[tuple[object, ...], BeforeValidator(to_tuple)]  # generic placeholder


# ---------------------------------------------------------------------------
# source manifest
# ---------------------------------------------------------------------------

SOURCE_MANIFEST_SCHEMA: Final = "episode0-source-manifest-v1"
EPISODE_JSON_SCHEMA: Final = "real-episode-v1"


class Episode0SourceManifest(StrictModel):
    """Frozen identity of the real-01 source file."""

    schema_version: Literal["episode0-source-manifest-v1"]
    video_path: str
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=0, strict=True)]
    duration_seconds: float | None = Field(default=None, ge=0)
    duration_probed: bool


def _probe_duration(path: Path) -> tuple[float | None, bool]:  # noqa: PLR0911
    """Return (duration_seconds, probed) via ffprobe — never fabricate."""
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return None, False
    except subprocess.TimeoutExpired:
        return None, False
    if completed.returncode != 0:
        return None, False
    raw = completed.stdout.strip()
    if not raw:
        return None, False
    try:
        value = float(raw)
    except ValueError:
        return None, False
    if value < 0:
        return None, False
    return value, True


def compute_source_manifest(video_path: Path) -> Episode0SourceManifest:
    """Compute deterministic sha256/size/duration for ``video_path``."""
    if not video_path.is_file():
        raise FileNotFoundError(f"source not found: {video_path}")
    digest = sha256_file(video_path)
    size = video_path.stat().st_size
    duration, probed = _probe_duration(video_path)
    return Episode0SourceManifest(
        schema_version=SOURCE_MANIFEST_SCHEMA,
        video_path=str(video_path),
        sha256=digest,
        size_bytes=size,
        duration_seconds=duration,
        duration_probed=probed,
    )


def compute_manifest_for_episode_json(episode_json: Path) -> Episode0SourceManifest:
    """Read ``episode.json`` (real-episode-v1) for the video path and freeze it."""
    payload: object = json.loads(episode_json.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"episode.json is not an object: {episode_json}")
    video_name = payload.get("video_path")
    if not isinstance(video_name, str) or not video_name:
        raise ValueError(f"episode.json missing video_path: {episode_json}")
    video_path = episode_json.parent / video_name
    return compute_source_manifest(video_path)


# ---------------------------------------------------------------------------
# operator log schema — episode0-baseline-v1
# ---------------------------------------------------------------------------


class TimestampNote(StrictModel):
    ts: NonEmptyStr
    note: NonEmptyStr


class Publishability(StrictModel):
    publishable: bool
    comment: str
    best_ts: NonEmptyStr
    worst_ts: NonEmptyStr


class Episode0BaselineLogV1(StrictModel):
    """Operator-measured baseline for one Episode-0 run."""

    schema_version: Literal["episode0-baseline-v1"]
    active_human_time_minutes: NonNegativeFloat
    ttfrp_minutes: NonNegativeFloat
    wall_clock_minutes: NonNegativeFloat
    manual_resolve_minutes: NonNegativeFloat
    wrong_keep_remove: Annotated[tuple[TimestampNote, ...], BeforeValidator(to_tuple)]
    missed_moments: Annotated[tuple[TimestampNote, ...], BeforeValidator(to_tuple)]
    finishing_deficits: Annotated[tuple[TimestampNote, ...], BeforeValidator(to_tuple)]
    interruption_points: Annotated[tuple[TimestampNote, ...], BeforeValidator(to_tuple)]
    publishability: Publishability


# ---------------------------------------------------------------------------
# report (manifest + log) — stable canonical bytes
# ---------------------------------------------------------------------------

REPORT_SCHEMA: Final = "episode0-report-v1"


class Episode0ReportV1(StrictModel):
    schema_version: Literal["episode0-report-v1"]
    run_id: NonEmptyStr
    manifest: Episode0SourceManifest
    log: Episode0BaselineLogV1


def generate_report(
    log: Episode0BaselineLogV1,
    manifest: Episode0SourceManifest,
    *,
    run_id: str,
    runs_root: Path,
) -> Path:
    """Write ``runs_root/<run_id>/report.json`` with canonical bytes."""
    if not run_id or "/" in run_id or "\\" in run_id:
        raise ValueError(f"invalid run_id: {run_id!r}")
    report = Episode0ReportV1(
        schema_version=REPORT_SCHEMA,
        run_id=run_id,
        manifest=manifest,
        log=log,
    )
    out = runs_root / run_id / "report.json"
    atomic_write(out, canonical_model_bytes(report))
    return out


def load_report(path: Path) -> Episode0ReportV1:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    return Episode0ReportV1.model_validate(payload)


def compare_reports(a_path: Path, b_path: Path) -> dict[str, object]:
    """Delta summary between two report.json files."""
    a = load_report(a_path)
    b = load_report(b_path)

    def _delta(a_val: float, b_val: float) -> dict[str, float]:
        return {"a": a_val, "b": b_val, "delta": b_val - a_val}

    deltas: dict[str, object] = {
        "a_run_id": a.run_id,
        "b_run_id": b.run_id,
        "active_human_time_minutes": _delta(
            float(a.log.active_human_time_minutes),
            float(b.log.active_human_time_minutes),
        ),
        "ttfrp_minutes": _delta(float(a.log.ttfrp_minutes), float(b.log.ttfrp_minutes)),
        "wall_clock_minutes": _delta(
            float(a.log.wall_clock_minutes), float(b.log.wall_clock_minutes)
        ),
        "manual_resolve_minutes": _delta(
            float(a.log.manual_resolve_minutes), float(b.log.manual_resolve_minutes)
        ),
        "wrong_keep_remove_count": {
            "a": len(a.log.wrong_keep_remove),
            "b": len(b.log.wrong_keep_remove),
            "delta": len(b.log.wrong_keep_remove) - len(a.log.wrong_keep_remove),
        },
        "missed_moments_count": {
            "a": len(a.log.missed_moments),
            "b": len(b.log.missed_moments),
            "delta": len(b.log.missed_moments) - len(a.log.missed_moments),
        },
        "finishing_deficits_count": {
            "a": len(a.log.finishing_deficits),
            "b": len(b.log.finishing_deficits),
            "delta": len(b.log.finishing_deficits) - len(a.log.finishing_deficits),
        },
        "interruption_points_count": {
            "a": len(a.log.interruption_points),
            "b": len(b.log.interruption_points),
            "delta": len(b.log.interruption_points) - len(a.log.interruption_points),
        },
        "publishability_changed": (
            a.log.publishability.publishable != b.log.publishability.publishable
        ),
        "publishability": {
            "a": a.log.publishability.model_dump(mode="json"),
            "b": b.log.publishability.model_dump(mode="json"),
        },
        "manifest_sha_changed": a.manifest.sha256 != b.manifest.sha256,
    }
    return deltas


__all__ = [
    "Episode0BaselineLogV1",
    "Episode0ReportV1",
    "Episode0SourceManifest",
    "Publishability",
    "TimestampNote",
    "ValidationError",
    "compare_reports",
    "compute_manifest_for_episode_json",
    "compute_source_manifest",
    "generate_report",
    "load_report",
]
