"""Golden-table parsers for the Phase-1 Technical Gate.

The independently derived golden tables come in two shapes — plan-style rows
(``span`` record span + ``source_span``) and abstract IR rows (flat
``record_start``/``source_start`` fields, no A/V link labels). Both normalize
to the shared :class:`ItemRow` form (sorted per kind by record start) so the
parity comparison in ``gate_p1_parity`` can consume either shape.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from services.job_runner.gate_p1_parity import (
    KINDS,
    ItemRow,
    ParityError,
    _as_dict,
    _as_int,
    _as_list,
)


def rows_from_golden_plan(
    plan_rows: list[Mapping[str, object]],
) -> dict[str, list[ItemRow]]:
    rows: dict[str, list[ItemRow]] = {kind: [] for kind in KINDS}
    for entry in plan_rows:
        span = _as_dict(entry["span"], "golden.span")
        source = _as_dict(entry["source_span"], "golden.source_span")
        kind = str(entry["kind"])
        rows.setdefault(kind, []).append(
            ItemRow(
                item_id=str(entry["item_id"]),
                kind=kind,
                source_start=_as_int(source["start_frame"], "source start"),
                source_end=_as_int(source["end_frame"], "source end"),
                record_start=_as_int(span["start_frame"], "record start"),
                record_end=_as_int(span["end_frame"], "record end"),
                track_index=_as_int(entry["track_index"], "track index"),
                subtitle_text=(
                    str(entry["subtitle_text"])
                    if entry.get("subtitle_text") is not None
                    else None
                ),
                av_link=(
                    str(entry["av_link_id"]) if entry.get("av_link_id") is not None else None
                ),
            )
        )
    for kind, kind_rows in rows.items():
        rows[kind] = sorted(kind_rows, key=lambda row: (row.record_start, row.item_id))
    return rows


def rows_from_golden_ir(
    ir_rows: list[Mapping[str, object]],
) -> dict[str, list[ItemRow]]:
    rows: dict[str, list[ItemRow]] = {kind: [] for kind in KINDS}
    for entry in ir_rows:
        kind = str(entry["kind"])
        rows.setdefault(kind, []).append(
            ItemRow(
                item_id=str(entry["item_id"]),
                kind=kind,
                source_start=_as_int(entry["source_start"], "source start"),
                source_end=_as_int(entry["source_end"], "source end"),
                record_start=_as_int(entry["record_start"], "record start"),
                record_end=_as_int(entry["record_end"], "record end"),
                track_index=_as_int(entry["track_index"], "track index"),
                subtitle_text=(
                    str(entry["subtitle_text"]) if entry.get("subtitle_text") is not None else None
                ),
                av_link=None,
            )
        )
    for kind, kind_rows in rows.items():
        rows[kind] = sorted(kind_rows, key=lambda row: (row.record_start, row.item_id))
    return rows


def _golden_rows(
    golden_fixture: Mapping[str, object], key: str, label: str
) -> list[Mapping[str, object]]:
    rows = _as_list(golden_fixture[key], label)
    return [_as_dict(row, f"{label} row") for row in rows]


def load_plan_golden(
    golden_fixture: Mapping[str, object], key: str
) -> dict[str, list[ItemRow]]:
    return rows_from_golden_plan(_golden_rows(golden_fixture, key, key))


def load_ir_golden(golden_fixture: Mapping[str, object], key: str) -> dict[str, list[ItemRow]]:
    return rows_from_golden_ir(_golden_rows(golden_fixture, key, key))


def load_golden_document(path: Path) -> dict[str, object]:
    document = json.loads(path.read_bytes())
    if not isinstance(document, dict):
        raise ParityError("malformed-evidence", f"golden document is not an object: {path}")
    return document


__all__ = [
    "load_golden_document",
    "load_ir_golden",
    "load_plan_golden",
    "rows_from_golden_ir",
    "rows_from_golden_plan",
]
