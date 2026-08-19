"""Expected-vs-observed evidence lines for drift reports.

One human-readable line per faulted item on each side (expected vs
observed), plus the structured per-item fault records the DriftReport
carries for the human routes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.contracts.primitives import StrictModel

if TYPE_CHECKING:
    from services.build.conformance_models import ConformanceTable, ExtraRow, ItemVerdict


class DriftItemFault(StrictModel):
    """One faulted item with its expected/observed evidence lines."""

    item_id: str
    faults: tuple[str, ...]
    expected: str
    observed: str


def expected_line(verdict: ItemVerdict) -> str:
    return (
        f"{verdict.item_id}: media={verdict.media_path_expected}:"
        f"{verdict.media_sha256_expected[:12]} "
        f"source=[{verdict.source_start_expected},{verdict.source_end_expected}) "
        f"record=[{verdict.record_start_expected},{verdict.record_end_expected}) "
        f"track={verdict.track_kind_expected}{verdict.track_index_expected} "
        f"links={list(verdict.linked_expected)}"
    )


def observed_line(verdict: ItemVerdict) -> str:
    return (
        f"{verdict.item_id}: media={verdict.media_path_observed}:"
        f"{verdict.media_sha256_observed[:12]} "
        f"source=[{verdict.source_start_observed},{verdict.source_end_observed}) "
        f"record=[{verdict.record_start_observed},{verdict.record_end_observed}) "
        f"track={verdict.track_kind_observed}{verdict.track_index_observed} "
        f"links={list(verdict.linked_observed)}"
    )


def extra_line(extra: ExtraRow) -> str:
    return (
        f"{extra.observed_id}: kind={extra.kind} "
        f"record=[{extra.record_start},{extra.record_end}) media={extra.media_path}"
    )


def evidence_lines(
    table: ConformanceTable,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[DriftItemFault, ...]]:
    """(expected, observed, item_faults) evidence for a drifted table."""

    expected: list[str] = []
    observed: list[str] = []
    faults: list[DriftItemFault] = []
    for verdict in table.items:
        if verdict.passed:
            continue
        observed_one = (
            "absent from timeline" if not verdict.observed else observed_line(verdict)
        )
        expected.append(expected_line(verdict))
        observed.append(observed_one)
        faults.append(
            DriftItemFault(
                item_id=verdict.item_id,
                faults=tuple(verdict.faults),
                expected=expected_line(verdict),
                observed=observed_one,
            )
        )
    for extra in table.extra_rows:
        line = extra_line(extra)
        expected.append(f"{extra.observed_id}: not declared by the package")
        observed.append(line)
        faults.append(
            DriftItemFault(
                item_id=extra.observed_id,
                faults=("extra_item",),
                expected="not declared by the package",
                observed=line,
            )
        )
    return tuple(expected), tuple(observed), tuple(faults)


__all__ = [
    "DriftItemFault",
    "evidence_lines",
    "expected_line",
    "extra_line",
    "observed_line",
]
