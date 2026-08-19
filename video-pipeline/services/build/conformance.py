"""Item-level Package↔Resolve conformance verification.

The :class:`ConformanceChecker` associates every observed readback row with
exactly one package placement and verifies EVERY item individually; the
aggregate counts/totals and both timeline fingerprints are computed
alongside and never substitute for the per-item table (the models make a
totals-only table structurally impossible). ``verify_built_conformance``
is the post-build entry: capture, verify, and refuse any failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.build.builder_models import BuildFailure, ItemReadbackRow
from services.build.conformance_associate import associate, observed_projection
from services.build.conformance_capture import TimelineReadback, capture_readback
from services.build.conformance_fingerprint import (
    conformance_fingerprint,
    expected_fingerprint,
    expected_placements,
    link_partners,
)
from services.build.conformance_models import ConformanceTable, ExtraRow
from services.build.conformance_verdict import build_verdict

if TYPE_CHECKING:
    from services.build.conformance_capture import Sha256Of
    from services.resolve_adapter.models import ResolvePackage
    from services.resolve_bridge.base_cut_models import BaseCutTimelineApi


class ConformanceChecker:
    """Verify a Resolve Package against an observed readback, item by item."""

    def verify(self, package: ResolvePackage, readback: TimelineReadback) -> ConformanceTable:
        declared = {binding.source_id: binding for binding in package.inputs_view.declared_media}
        for placement in package.placements:
            source_id = placement.clip_info.media_source_id
            if source_id not in declared:
                raise BuildFailure(
                    "readback-mismatch", f"{placement.item_id}: unbound media source {source_id}"
                )
        partners = link_partners(package)
        pairing = associate(package, readback.rows)
        verdicts = tuple(
            build_verdict(
                placement,
                declared[placement.clip_info.media_source_id],
                partners.get(placement.item_id, frozenset()),
                pairing,
            )
            for placement in package.placements
        )
        extras = tuple(
            ExtraRow(
                observed_id=pairing.item_id_of_row(row.unique_id),
                unique_id=row.unique_id,
                kind=row.kind,
                record_start=row.record_start,
                record_end=row.record_end,
                media_path=row.media_path,
            )
            for row in pairing.extra_rows
        )
        expected_total = sum(
            p.clip_info.end_frame - p.clip_info.start_frame for p in package.placements
        )
        observed_total = sum(row.record_end - row.record_start for row in readback.rows)
        return ConformanceTable(
            timeline_name=readback.timeline_name,
            package_artifact_id=package.artifact_id,
            items=verdicts,
            extra_rows=extras,
            expected_items=len(package.placements),
            observed_items=len(readback.rows),
            missing_item_ids=tuple(v.item_id for v in verdicts if not v.observed),
            expected_total_record_frames=expected_total,
            observed_total_record_frames=observed_total,
            total_record_delta_frames=observed_total - expected_total,
            expected_fingerprint=expected_fingerprint(package),
            observed_fingerprint=conformance_fingerprint(
                observed_projection(readback.rows, pairing)
            ),
            all_passed=bool(verdicts) and not extras and all(v.passed for v in verdicts),
        )


def item_readback_rows(table: ConformanceTable) -> tuple[ItemReadbackRow, ...]:
    """Project the conformance verdicts onto the Todo-48 readback rows."""

    rows: list[ItemReadbackRow] = []
    for verdict in table.items:
        if not verdict.observed:
            raise BuildFailure(
                "readback-mismatch", f"{verdict.item_id}: no placed item to read back"
            )
        rows.append(
            ItemReadbackRow(
                item_id=verdict.item_id,
                kind=verdict.track_kind_observed,
                track_index=verdict.track_index_observed,
                record_start=verdict.record_start_observed,
                record_end=verdict.record_end_observed,
                source_start=verdict.source_start_observed,
                source_end=verdict.source_end_observed,
                media_path=verdict.media_path_observed,
                linked_ids=verdict.linked_observed,
                passed=verdict.passed,
                detail=verdict.detail,
            )
        )
    return tuple(rows)


def verify_built_conformance(
    timeline: BaseCutTimelineApi, package: ResolvePackage, sha256_of: Sha256Of
) -> ConformanceTable:
    """Post-build verification: capture, verify every item, refuse failures."""

    table = ConformanceChecker().verify(package, capture_readback(timeline, sha256_of))
    if not table.all_passed:
        failed = "; ".join(
            f"{verdict.item_id}: {verdict.detail}"
            for verdict in table.items
            if not verdict.passed
        )
        extras = ", ".join(extra.observed_id for extra in table.extra_rows)
        raise BuildFailure("readback-mismatch", f"{failed}; extra rows: {extras}")
    return table


__all__ = [
    "ConformanceChecker",
    "capture_readback",
    "expected_fingerprint",
    "expected_placements",
    "item_readback_rows",
    "verify_built_conformance",
]
