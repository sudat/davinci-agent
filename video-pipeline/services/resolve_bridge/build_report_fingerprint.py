"""Item-table assembly and the deterministic timeline fingerprint.

The canonical serialization is defined precisely so any verifier recomputes
the same fingerprint: observed placements are ordered by
``(record_span.start_frame, kind rank [video=0, audio=1], item_id)`` and each
placement becomes one ``|``-separated line

``item_id|kind|source_id|src_start|src_end|num/den|rec_start|rec_end|
track_kind|track_index|av_link_id-or-dash|media_path``

joined with ``\\n`` and terminated by a trailing ``\\n``; the fingerprint is
``sha256`` over those UTF-8 bytes.
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING, Final

from services.contracts.build_report import BuildItemEvidence0A, ItemPlacement0A
from services.contracts.primitives import (
    RationalFrameRate,
    RecordFrameSpan,
    Sha256,
    SourceFrameSpan,
    SourceRef,
    TrackKind,
    TrackRef,
)
from services.resolve_bridge.base_cut_plan import source_id_for

if TYPE_CHECKING:
    from services.resolve_bridge.base_cut_models import ReadItem, TimelineSnapshot
    from services.resolve_bridge.base_cut_plan import ExpectedBaseCut, ExpectedItem

KIND_RANK: Final[dict[TrackKind, int]] = {"video": 0, "audio": 1}
FIELD_SEPARATOR: Final = "|"
NO_LINK: Final = "-"


def requested_placement(
    want: ExpectedItem, rate: RationalFrameRate, origin: int
) -> ItemPlacement0A:
    return ItemPlacement0A(
        item_id=want.item_id,
        kind=want.kind,
        source=SourceRef(
            source_id=source_id_for(want.av_link_id),
            span=SourceFrameSpan(
                start_frame=want.source_start, end_frame=want.source_end, rate=rate
            ),
        ),
        record_span=RecordFrameSpan(
            start_frame=want.record_start + origin, end_frame=want.record_end + origin
        ),
        track=TrackRef(kind=want.kind, index=want.track_index),
        av_link_id=want.av_link_id,
        media_path=os.path.realpath(want.media_path),
    )


def observed_placement(
    built: ReadItem, want: ExpectedItem, rate: RationalFrameRate, *, link_ok: bool
) -> ItemPlacement0A:
    return ItemPlacement0A(
        item_id=want.item_id,
        kind=built.kind,
        source=SourceRef(
            source_id=source_id_for(want.av_link_id),
            span=SourceFrameSpan(
                start_frame=built.source_start, end_frame=built.source_end, rate=rate
            ),
        ),
        record_span=RecordFrameSpan(
            start_frame=built.record_start, end_frame=built.record_end
        ),
        track=TrackRef(kind=built.kind, index=built.track_index),
        av_link_id=want.av_link_id if link_ok else None,
        media_path=os.path.realpath(built.media_path),
    )


def item_evidence_rows(
    expected: ExpectedBaseCut, snapshot: TimelineSnapshot, rate: RationalFrameRate, origin: int
) -> tuple[BuildItemEvidence0A, ...]:
    rows: list[BuildItemEvidence0A] = []
    for want in sorted(expected.items, key=lambda item: item.record_start):
        built = next(
            (
                item
                for item in snapshot.items
                if item.kind == want.kind and item.record_start == want.record_start + origin
            ),
            None,
        )
        if built is None:
            continue
        partner = next(
            (
                item
                for item in snapshot.items
                if item.kind != want.kind and item.record_start == built.record_start
            ),
            None,
        )
        link_ok = partner is not None and built.linked_ids == frozenset({partner.unique_id})
        rows.append(
            BuildItemEvidence0A(
                requested=requested_placement(want, rate, origin),
                observed=observed_placement(built, want, rate, link_ok=link_ok),
            )
        )
    return tuple(rows)


def fingerprint_bytes(placements: tuple[ItemPlacement0A, ...]) -> bytes:
    ordered = sorted(
        placements,
        key=lambda item: (item.record_span.start_frame, KIND_RANK[item.kind], item.item_id),
    )
    lines = [fingerprint_line(item) for item in ordered]
    return ("\n".join(lines) + "\n").encode()


def fingerprint_line(item: ItemPlacement0A) -> str:
    return FIELD_SEPARATOR.join(
        (
            item.item_id,
            item.kind,
            item.source.source_id,
            str(item.source.span.start_frame),
            str(item.source.span.end_frame),
            f"{item.source.span.rate.num}/{item.source.span.rate.den}",
            str(item.record_span.start_frame),
            str(item.record_span.end_frame),
            item.track.kind,
            str(item.track.index),
            item.av_link_id if item.av_link_id is not None else NO_LINK,
            item.media_path,
        )
    )


def timeline_fingerprint(placements: tuple[ItemPlacement0A, ...]) -> Sha256:
    return hashlib.sha256(fingerprint_bytes(placements)).hexdigest()
