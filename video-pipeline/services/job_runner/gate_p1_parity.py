"""Golden parity recomputation for the Phase-1 Technical Gate.

Normalizes raw review-store plan/IR JSON into per-kind record-ordered rows and
compares them against the independently derived golden tables. The goldens
label video items ``v{N}``, audio ``a{N}``, and subtitles ``st{N}``
(positionally, in record order); the review plane carries the SEGMENT id on
video items (``s*``), ``a{position}`` audio, and the manifest's subtitle ids —
the Todo-45 disclosed mapping compared here positionally per kind. A/V linking
is compared as partner position pairs, never as raw link labels (the goldens
keep manifest link ids; the review plane regenerates ``av{position}``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

KINDS = ("video", "audio", "subtitle")


@dataclass(frozen=True, slots=True)
class ItemRow:
    item_id: str
    kind: str
    source_start: int
    source_end: int
    record_start: int
    record_end: int
    track_index: int
    subtitle_text: str | None
    av_link: str | None

    def coords(self) -> tuple[object, ...]:
        return (
            self.kind,
            self.source_start,
            self.source_end,
            self.record_start,
            self.record_end,
            self.track_index,
            self.subtitle_text,
        )


def rows_from_ir(ir_document: Mapping[str, object]) -> dict[str, list[ItemRow]]:
    tracks = _as_list(ir_document["tracks"], "ir.tracks")
    rows: dict[str, list[ItemRow]] = {kind: [] for kind in KINDS}
    for track_entry in tracks:
        track = _as_dict(track_entry, "ir.track")
        track_ref = _as_dict(track["track"], "ir.track.track")
        items = _as_list(track["items"], "ir.track.items")
        for item_entry in items:
            item = _as_dict(item_entry, "ir.item")
            source = _as_dict(item["source"], "ir.item.source")
            span = _as_dict(source["span"], "ir.item.source.span")
            record = _as_dict(item["record_span"], "ir.item.record_span")
            kind = str(item["kind"])
            row = ItemRow(
                item_id=str(item["item_id"]),
                kind=kind,
                source_start=_as_int(span["start_frame"], "source start"),
                source_end=_as_int(span["end_frame"], "source end"),
                record_start=_as_int(record["start_frame"], "record start"),
                record_end=_as_int(record["end_frame"], "record end"),
                track_index=_as_int(track_ref["index"], "track index"),
                subtitle_text=(
                    str(item["subtitle_text"])
                    if item.get("subtitle_text") is not None
                    else None
                ),
                av_link=(
                    str(item["av_link_id"]) if item.get("av_link_id") is not None else None
                ),
            )
            rows.setdefault(kind, []).append(row)
    for kind, kind_rows in rows.items():
        rows[kind] = sorted(kind_rows, key=lambda row: (row.record_start, row.item_id))
    return rows


def av_pairs(rows: dict[str, list[ItemRow]]) -> set[tuple[int, int]]:
    """Position pairs (video_pos, audio_pos) linked by a shared av link id."""

    audio_pos = {row.av_link: pos for pos, row in enumerate(rows["audio"], start=1)}
    return {
        (pos, audio_pos[row.av_link])
        for pos, row in enumerate(rows["video"], start=1)
        if row.av_link is not None and row.av_link in audio_pos
    }


class ParityError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _as_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ParityError("malformed-evidence", f"{label} is not an object: {value!r}")
    return value


def _as_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ParityError("malformed-evidence", f"{label} is not an array: {value!r}")
    return value


def _as_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ParityError("malformed-evidence", f"{label} is not an integer: {value!r}")
    return value


def compare(
    actual: dict[str, list[ItemRow]], golden: dict[str, list[ItemRow]], *, av_labels: bool = True
) -> None:
    """Raise ParityError on any coordinate/placement divergence.

    ``av_labels=False`` skips partner-pair comparison for golden tables that
    carry no A/V link labels (the abstract ``ir_records`` projection).
    """

    for kind in KINDS:
        actual_rows = actual[kind]
        golden_rows = golden[kind]
        if len(actual_rows) != len(golden_rows):
            raise ParityError(
                "item-count-mismatch",
                f"{kind}: actual {len(actual_rows)} != golden {len(golden_rows)}",
            )
        for position, (row, expected) in enumerate(
            zip(actual_rows, golden_rows, strict=True), start=1
        ):
            if row.coords() != expected.coords():
                raise ParityError(
                    "coordinate-defect",
                    f"{kind}#{position}: actual {row.coords()} != golden {expected.coords()}",
                )
    if av_labels and av_pairs(actual) != av_pairs(golden):
        raise ParityError(
            "av-pairing-defect",
            f"av partner pairs diverge: {sorted(av_pairs(actual))} != {sorted(av_pairs(golden))}",
        )


def check_ordering(rows: dict[str, list[ItemRow]]) -> None:
    for kind in KINDS:
        starts = [row.record_start for row in rows[kind]]
        if starts != sorted(starts) or len(set(starts)) != len(starts):
            raise ParityError("record-order-unstable", f"{kind} record starts not increasing")
    video_sources = [row.source_start for row in rows["video"]]
    if video_sources != sorted(video_sources):
        raise ParityError(
            "source-order-unstable", "video record order does not follow source order"
        )


def load_ir(path: Path) -> dict[str, list[ItemRow]]:
    document = json.loads(path.read_bytes())
    return rows_from_ir(_as_dict(document, "ir document"))


__all__ = [
    "KINDS",
    "ItemRow",
    "ParityError",
    "_as_dict",
    "_as_int",
    "_as_list",
    "av_pairs",
    "check_ordering",
    "compare",
    "load_ir",
    "rows_from_ir",
]
