"""Live-readback verification and sync-row collection for the 0B evaluator.

``check_readback`` re-derives the expected item set from the frozen fixture
manifest (whole clip + one anchor per marker, in order, at recomputed record
positions) and refuses to trust the report's own requested fields; stale host
or Resolve-build bindings and nonzero frame deltas return Stop criteria.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from services.resolve_bridge.fixed_presentation_models import FRAME_ORIGIN
from services.spike.gate_phase0b_checks import CODE_STALE, CheckState
from services.spike.gate_phase0b_models import (
    ANCHOR_PREFIXES,
    ANCHOR_WINDOW_FRAMES,
    CRITERION_ANCHORS,
    CRITERION_READBACK,
    STOP_STALE_BINDING,
    STOP_UNCLASSIFIED,
    SYNC_PREFIXES,
    LiveReadbackReport,
    ReadbackBindings,
    SyncRow,
)
from services.spike.gate_phase0b_sync import measure_variant

if TYPE_CHECKING:
    from services.conform.map_models import ConformMap
    from services.fixtures.manifest_phase0b import Phase0BFixtureManifest
    from services.ingest.models import SourceManifest
    from services.normalize.models import NormalizeRecord

CODE_SYNC_OVER: Final = "sync-over-one-frame"


def _build_matches(host_build: str | None, bindings: ReadbackBindings) -> bool:
    """Mirror ``connection._build_matches``: 5 == 21.0.40005 for core 21.0.4."""

    reported = bindings.resolve_build
    core = bindings.resolve_version
    if host_build is None or host_build == reported:
        return True
    if reported.isdigit():
        live = int(reported)
        return (host_build.isdigit() and int(host_build) == live) or (
            f"{core}{live:04d}" == host_build
        )
    return host_build == reported
CODE_DELTA: Final = "readback-frame-delta-nonzero"


def check_readback(
    variant: str,
    fixture: Phase0BFixtureManifest,
    record: NormalizeRecord,
    report: LiveReadbackReport | None,
    current_host_sha: str | None,
    host_version: str | None,
    host_build: str | None,
    manifest: SourceManifest | None,
    state: CheckState,
) -> tuple[str, str]:
    if report is None:
        state.fail(CRITERION_READBACK, "missing-readback-evidence", variant)
        return "", ""
    output_frames = fixture.conversions["cfr30"].output_frames
    bindings = report.bindings
    if (
        manifest is None
        or bindings.source_manifest_sha256 != manifest.content_hash
        or bindings.normalize_record_sha256 != record.content_hash
        or bindings.edit_source_sha256 != record.output.sha256
    ):
        state.fail(CRITERION_READBACK, CODE_STALE, f"{variant}: readback chain drift")
    if current_host_sha is not None and bindings.host_report_sha256 != current_host_sha:
        state.fail(CRITERION_READBACK, CODE_STALE, f"{variant}: stale host binding")
        return (
            STOP_STALE_BINDING,
            (
                f"readback for {variant} is bound to a different host report/build "
                "than the current one"
            ),
        )
    if host_version is not None and (
        bindings.resolve_version != host_version or not _build_matches(host_build, bindings)
    ):
        state.fail(CRITERION_READBACK, CODE_STALE, f"{variant}: stale Resolve build binding")
        return (
            STOP_STALE_BINDING,
            (
                f"readback for {variant} was taken on Resolve {bindings.resolve_version} "
                f"build {bindings.resolve_build} != current {host_version}/{host_build}"
            ),
        )
    if report.frame_origin != FRAME_ORIGIN:
        state.fail(
            CRITERION_READBACK,
            "frame-origin-drift",
            f"{variant}: origin {report.frame_origin} != {FRAME_ORIGIN}",
        )
    expected_ids = ("whole-clip", *(m.marker_id for m in fixture.resolve_readback))
    actual_ids = tuple(item.requested.item_id for item in report.items)
    if actual_ids != expected_ids:
        state.fail(
            CRITERION_READBACK,
            "readback-coverage-mismatch",
            f"{variant}: items {actual_ids} != frozen markers {expected_ids}",
        )
        return "", ""
    cursor = FRAME_ORIGIN + output_frames
    for item, marker in zip(
        report.items[1:], fixture.resolve_readback, strict=True
    ):
        start = marker.cfr30_frame if marker.cfr30_frame is not None else -1
        end = min(start + ANCHOR_WINDOW_FRAMES, output_frames)
        if (
            item.requested.source_start != start
            or item.requested.source_end != end
            or item.requested.record_start != cursor
        ):
            state.fail(
                CRITERION_READBACK,
                "readback-anchor-unbound",
                f"{variant}/{item.requested.item_id}: requested anchor != frozen manifest",
            )
        cursor += end - start
    if report.items[0].requested.source_start != 0 or (
        report.items[0].requested.source_end != output_frames
    ):
        state.fail(CRITERION_READBACK, "whole-clip-unbound", variant)
    max_delta = max(item.frame_delta for item in report.items)
    if max_delta != 0:
        state.fail(CRITERION_READBACK, CODE_DELTA, f"{variant}: max frame delta {max_delta}")
        return (
            STOP_UNCLASSIFIED,
            (
                f"live readback for {variant} shows an unclassified frame mismatch "
                f"(max delta {max_delta}); never proceed on unclassified mismatches"
            ),
        )
    return "", ""


def collect_sync(
    variant: str, fixture: Phase0BFixtureManifest, conform_map: ConformMap, state: CheckState
) -> tuple[SyncRow, ...]:
    rows = measure_variant(variant, fixture, conform_map)
    for row in rows:
        criterion = next(
            (
                criterion
                for prefix, criterion in (*ANCHOR_PREFIXES, *SYNC_PREFIXES)
                if row.name.startswith(prefix)
            ),
            None,
        )
        if criterion is None:
            state.fail(CRITERION_ANCHORS, "sync-row-unclassified", f"{variant}/{row.name}")
        elif not row.passed:
            code = CODE_SYNC_OVER if row.kind == "tolerance-one-frame" else "anchor-mismatch"
            state.fail(
                criterion, code, f"{variant}/{row.name}: delta {row.delta.num}/{row.delta.den}"
            )
    return rows
