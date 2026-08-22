"""Backend parity diff harness (stage 1: legacy-only).

The harness extracts a stable structural summary from the legacy
``ResolvePackage`` produced by :mod:`services.resolve_adapter.package`
and diffs two summaries field-by-field.  The MCP backend is a stub
until task 38/39; :func:`run_parity` is structured with a backend
registry so a second compiler can plug in without changing callers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from services.contracts.timeline_ir import TimelineIrProduction
from services.foundation_io import atomic_write, sha256_file
from services.resolve_adapter.models import MediaBinding, ResolvePackage
from services.resolve_adapter.package import PackageCompileRequest, compile_resolve_package
from services.toolchain.models import Phase2ToolchainLock, load_lock

# ---------------------------------------------------------------------------
# Backend registry — only ``legacy`` is wired in stage 1.
# ---------------------------------------------------------------------------

type BackendBuilder = Callable[[TimelineIrProduction], dict[str, Any]]

_BACKENDS: dict[str, BackendBuilder] = {}


def _register_backend(name: str, builder: BackendBuilder) -> None:
    _BACKENDS[name] = builder


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_REQUIRED_KEYS: tuple[str, ...] = (
    "source_ids",
    "source_in_out",
    "record_positions",
    "track_mapping",
    "duration",
    "render_properties",
)


def extract_structure(package: ResolvePackage) -> dict[str, Any]:
    """Derive the canonical parity structure from a :class:`ResolvePackage`.

    Returns a dict with **exactly** six keys required by the parity
    contract: ``source_ids``, ``source_in_out``, ``record_positions``,
    ``track_mapping``, ``duration``, ``render_properties``.
    """

    sorted_placements = sorted(package.placements, key=lambda p: p.clip_info.record_frame)

    source_ids: list[str] = sorted({p.media_source_id for p in package.placements})

    source_in_out: list[dict[str, Any]] = [
        {
            "end": p.clip_info.end_frame,
            "source_id": p.media_source_id,
            "start": p.clip_info.start_frame,
        }
        for p in sorted_placements
    ]

    record_positions: list[dict[str, Any]] = [
        {
            "record_end": p.clip_info.record_frame
            + (p.clip_info.end_frame - p.clip_info.start_frame),
            "record_start": p.clip_info.record_frame,
            "source_id": p.media_source_id,
        }
        for p in sorted_placements
    ]

    track_mapping: dict[str, Any] = {}
    for entry in package.track_map:
        dumped = entry.model_dump(mode="json")
        key = f"{dumped['logical_kind']}:{dumped['logical_index']}"
        # Summarise the Resolve binding without leaking internal ids.
        if dumped.get("placement") is not None:
            track_mapping[key] = {"placement": dumped["placement"]}
        elif dumped.get("role") is not None:
            track_mapping[key] = {
                "resolve_track_index": dumped["resolve_track_index"],
                "resolve_track_type": dumped["resolve_track_type"],
                "role": dumped["role"],
            }
        else:
            track_mapping[key] = {
                "resolve_track_index": dumped["resolve_track_index"],
                "resolve_track_type": dumped["resolve_track_type"],
            }

    duration: int = package.render_job.extent_frames

    render_properties: dict[str, Any] = {
        "audio_channels": package.render_job.audio_channels,
        "audio_codec": package.render_job.audio_codec,
        "audio_sample_rate": package.render_job.audio_sample_rate,
        "frame_rate": {
            "den": package.render_job.frame_rate.den,
            "num": package.render_job.frame_rate.num,
        },
        "height": package.render_job.height,
        "video_codec": package.render_job.video_codec,
        "video_format": package.render_job.video_format,
        "width": package.render_job.width,
    }

    structure: dict[str, Any] = {
        "duration": duration,
        "record_positions": record_positions,
        "render_properties": render_properties,
        "source_ids": source_ids,
        "source_in_out": source_in_out,
        "track_mapping": track_mapping,
    }
    # Defensive: never leak extra keys.
    if set(structure.keys()) != set(_REQUIRED_KEYS):
        msg = f"parity structure keys drift: {sorted(structure.keys())}"
        raise ValueError(msg)
    if tuple(sorted(structure.keys())) != tuple(sorted(_REQUIRED_KEYS)):
        msg = f"parity structure key order drift: {sorted(structure.keys())}"
        raise ValueError(msg)
    return structure


def diff_structures(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare two parity structures field-by-field.

    Returns a list of ``{field, a, b}`` records; empty when identical.
    Ordering follows the canonical key order so the report is stable.
    """

    differences: list[dict[str, Any]] = []
    for field in sorted(_REQUIRED_KEYS):
        aval = a.get(field)
        bval = b.get(field)
        # Use canonical JSON for deep comparison (handles nested ordering).
        if json.dumps(aval, sort_keys=True, separators=(",", ":")) != json.dumps(
            bval, sort_keys=True, separators=(",", ":")
        ):
            differences.append({"a": aval, "b": bval, "field": field})
    return differences


# ---------------------------------------------------------------------------
# IR loading
# ---------------------------------------------------------------------------


def _load_ir(
    ir_path_or_model: Path | str | TimelineIrProduction | dict[str, Any],
) -> TimelineIrProduction:
    if isinstance(ir_path_or_model, TimelineIrProduction):
        return ir_path_or_model
    if isinstance(ir_path_or_model, dict):
        return TimelineIrProduction.model_validate(ir_path_or_model, strict=False)
    path = Path(ir_path_or_model)
    raw = path.read_bytes()
    # StrictModel forbids list→tuple coercion; JSON arrays need lax parsing.
    payload: object = json.loads(raw)
    if not isinstance(payload, dict):
        msg = f"parity IR payload must be an object: {path}"
        raise TypeError(msg)
    return TimelineIrProduction.model_validate(payload, strict=False)


def _resolve_lock_path() -> Path:
    candidates = [
        Path.cwd() / "config/toolchains/phase-2-v1.json",
        Path(__file__).resolve().parents[2] / "config/toolchains/phase-2-v1.json",
        Path(__file__).resolve().parents[3] / "video-pipeline/config/toolchains/phase-2-v1.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _build_legacy_structure(ir: TimelineIrProduction) -> dict[str, Any]:
    lock_path = _resolve_lock_path()
    lock = load_lock(lock_path)
    if not isinstance(lock, Phase2ToolchainLock):
        msg = f"parity harness requires phase-2 lock at {lock_path}"
        raise TypeError(msg)
    lock_sha256 = sha256_file(lock_path)

    # Deterministic media bindings: one per unique A/V source_id in the IR.
    source_ids: list[str] = sorted(
        {
            item.source.source_id  # type: ignore[union-attr]
            for track in ir.tracks
            for item in track.items
            if hasattr(item, "source")
        }
    )
    # Map source_id -> max end_frame required by the IR (so duration check passes).
    max_end: dict[str, int] = {}
    for track in ir.tracks:
        for item in track.items:
            if not hasattr(item, "source"):  # type: ignore[union-attr]
                continue
            sid: str = item.source.source_id  # type: ignore[union-attr]
            end: int = item.source.span.end_frame  # type: ignore[union-attr]
            max_end[sid] = max(max_end.get(sid, 0), end)

    bindings: list[MediaBinding] = []
    for sid in source_ids:
        sha = hashlib.sha256(sid.encode()).hexdigest()
        duration = max(max_end.get(sid, 1), 1)
        # Add margin so overflow guard never trips on rounding
        duration = max(duration, 1)
        bindings.append(
            MediaBinding(
                source_id=sid,
                path=f"jobs/parity/sources/{sid}.mov",
                sha256=sha,
                duration_frames=duration,
            )
        )

    request = PackageCompileRequest(
        ir=ir,
        lock=lock,
        lock_sha256=lock_sha256,
        declared_media=tuple(bindings),
        artifact_id=f"resolve-package-parity-{ir.artifact_id}",
    )
    package = compile_resolve_package(request)
    return extract_structure(package)


# Register the only backend available in stage 1.
_register_backend("legacy", _build_legacy_structure)


def _structure_for_backend(backend: str, ir: TimelineIrProduction) -> dict[str, Any]:
    builder = _BACKENDS.get(backend)
    if builder is None:
        msg = f"unknown parity backend: {backend!r} (available: {sorted(_BACKENDS.keys())})"
        raise ValueError(msg)
    return builder(ir)


def run_parity(
    ir_path_or_model: Path | str | TimelineIrProduction | dict[str, Any],
    backend_a: str = "legacy",
    backend_b: str = "legacy",
    output_path: Path | str | None = None,
) -> dict[str, Any]:
    """Build two parity structures from the same IR and diff them.

    The report ``{differences, a_summary, b_summary}`` is written via
    :func:`atomic_write` to ``output_path`` (default
    ``./parity-report.json`` in the caller's cwd) and also returned.
    """

    ir = _load_ir(ir_path_or_model)
    a_summary = _structure_for_backend(backend_a, ir)
    b_summary = _structure_for_backend(backend_b, ir)
    differences = diff_structures(a_summary, b_summary)
    report: dict[str, Any] = {
        "a_summary": a_summary,
        "b_summary": b_summary,
        "differences": differences,
    }

    target = Path(output_path) if output_path is not None else Path.cwd() / "parity-report.json"
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    atomic_write(target, payload)
    return report


__all__ = ["diff_structures", "extract_structure", "run_parity"]
