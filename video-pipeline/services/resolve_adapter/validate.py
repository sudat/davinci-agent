"""Fail-closed input validation for Resolve-package compilation.

Every guard here runs BEFORE any instruction is emitted: the pinned
capability matrix must hash to the locked value with live-verified evidence
for all five required capabilities, the IR must be Resolve-field free and
hash-fresh, its track map must be the frozen video/1 audio/2 subtitle/3
layout, source rates must equal the timeline rate (retime is unsupported),
record length must equal source length (transitions/speed are unsupported),
and every media binding must match its declared sha256 with sufficient
duration extent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

from services.contracts.timeline_ir import (
    SubtitleCueItem,
    TimelineGapItem,
    TimelineIrProduction,
)
from services.foundation_io import canonical_model_bytes
from services.resolve_adapter.errors import (
    CAPABILITY_MATRIX_STALE,
    MATRIX_EVIDENCE_MISSING,
    MEDIA_BINDING_MISSING,
    MEDIA_EXTENT_OVERFLOW,
    MEDIA_HASH_DRIFT,
    RESOLVE_FIELD_IN_IR,
    STALE_IR,
    UNSUPPORTED_RETIME,
    UNSUPPORTED_TRANSITION,
    WRONG_TRACK_MAP,
    PackageCompileError,
)
from services.toolchain.resolve_package import ResolvePackageSmokeError, load_capability_matrix

if TYPE_CHECKING:
    from services.resolve_adapter.models import MediaBinding
    from services.toolchain.models import Phase2ToolchainLock

SUPPORTED_TRACKS: Final = frozenset({("video", 1), ("audio", 2), ("subtitle", 3)})
REQUIRED_CAPABILITIES: Final = (
    "base_cut",
    "fixed_subtitle",
    "media_intro_outro",
    "basic_audio_preset",
    "render",
)


def verify_matrix(lock: Phase2ToolchainLock) -> None:
    try:
        payload = load_capability_matrix(lock.resolve_package, Path.cwd().resolve())
    except ResolvePackageSmokeError as error:
        raise PackageCompileError(CAPABILITY_MATRIX_STALE, str(error)) from error
    rows: dict[str, dict[str, object]] = {}
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, list):
        for row in capabilities:
            if isinstance(row, dict) and isinstance(row.get("capability"), str):
                rows[str(row["capability"])] = row
    for capability in REQUIRED_CAPABILITIES:
        row = rows.get(capability)
        if row is None or row.get("live_verified") is not True:
            raise PackageCompileError(MATRIX_EVIDENCE_MISSING, f"not live-verified: {capability}")
        evidence = row.get("evidence_refs")
        if not isinstance(evidence, list) or not evidence:
            raise PackageCompileError(MATRIX_EVIDENCE_MISSING, f"no evidence refs: {capability}")


def scan_ir_for_resolve_fields(ir: TimelineIrProduction) -> None:
    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(key, str) and "resolve" in key.lower():
                    raise PackageCompileError(RESOLVE_FIELD_IN_IR, f"forbidden key: {key}")
                walk(child)
        elif isinstance(node, list | tuple):
            for child in node:
                walk(child)

    walk(json.loads(canonical_model_bytes(ir)))


def verify_ir_freshness(ir: TimelineIrProduction) -> None:
    digest = hashlib.sha256()
    digest.update(canonical_model_bytes(ir.rate))
    for track in ir.tracks:
        digest.update(canonical_model_bytes(track))
    if digest.hexdigest() != ir.content_hash:
        raise PackageCompileError(STALE_IR, "IR content hash does not match its tracks")


def verify_track_map(ir: TimelineIrProduction) -> None:
    for track in ir.tracks:
        if (track.track.kind, track.track.index) not in SUPPORTED_TRACKS:
            raise PackageCompileError(
                WRONG_TRACK_MAP,
                f"unsupported logical track {track.track.kind}/{track.track.index}",
            )


def verify_rates(ir: TimelineIrProduction) -> None:
    rate = ir.rate
    for track in ir.tracks:
        for item in track.items:
            if isinstance(item, TimelineGapItem):
                continue
            span_rate = item.source.span.rate
            if (span_rate.num, span_rate.den) != (rate.num, rate.den):
                raise PackageCompileError(
                    UNSUPPORTED_RETIME,
                    f"{item.item_id}: source rate {span_rate.num}/{span_rate.den} != "
                    f"timeline rate {rate.num}/{rate.den}; constant-speed retime is "
                    "unsupported in the MVP capability set",
                )
            if track.track.kind == "video" and item.record_span.length != item.source.span.length:
                raise PackageCompileError(
                    UNSUPPORTED_TRANSITION,
                    f"{item.item_id}: record length != source length implies "
                    "speed/transition effects outside the validated capability set",
                )


def verify_media(
    ir: TimelineIrProduction,
    declared_media: tuple[MediaBinding, ...],
    presented_media: tuple[MediaBinding, ...],
) -> None:
    declared = {binding.source_id: binding for binding in declared_media}
    presented = {binding.source_id: binding for binding in presented_media}
    if len(declared) != len(declared_media) or len(presented) != len(presented_media):
        raise PackageCompileError(MEDIA_BINDING_MISSING, "duplicate media source bindings")
    for track in ir.tracks:
        for item in track.items:
            if isinstance(item, TimelineGapItem | SubtitleCueItem):
                continue
            expected = declared.get(item.source.source_id)
            if expected is None:
                raise PackageCompileError(
                    MEDIA_BINDING_MISSING, f"no media binding for {item.source.source_id}"
                )
            actual = presented.get(item.source.source_id)
            if actual is not None and actual.sha256 != expected.sha256:
                raise PackageCompileError(
                    MEDIA_HASH_DRIFT,
                    f"{item.source.source_id}: presented sha256 {actual.sha256} != declared "
                    f"{expected.sha256} (identical duration is not sufficient)",
                )
            if item.source.span.end_frame > expected.duration_frames:
                raise PackageCompileError(
                    MEDIA_EXTENT_OVERFLOW,
                    f"{item.item_id}: source span ends at {item.source.span.end_frame} "
                    f"but declared media covers {expected.duration_frames} frames",
                )


def verify_compile_inputs(
    ir: TimelineIrProduction,
    lock: Phase2ToolchainLock,
    declared_media: tuple[MediaBinding, ...],
    presented_media: tuple[MediaBinding, ...],
) -> None:
    verify_matrix(lock)
    scan_ir_for_resolve_fields(ir)
    verify_ir_freshness(ir)
    verify_track_map(ir)
    verify_rates(ir)
    verify_media(ir, declared_media, presented_media)


__all__ = ["verify_compile_inputs"]
