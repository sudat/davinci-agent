"""Exact readback comparison of a built base cut against the expected plan."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from services.contracts.primitives import StrictModel, TrackKind
from services.resolve_bridge.base_cut_models import (  # noqa: TC001 - pydantic runtime
    TimelineSnapshot,
)
from services.resolve_bridge.base_cut_plan import (  # noqa: TC001 - pydantic runtime
    ExpectedBaseCut,
)

if TYPE_CHECKING:
    from services.resolve_bridge.base_cut_models import ReadItem
    from services.resolve_bridge.base_cut_plan import ExpectedItem

KINDS: Final = ("video", "audio")
CODE_ITEM_COUNT = "item-count-mismatch"
CODE_TRACK_COUNT = "track-count-mismatch"
CODE_RECORD_LENGTH = "record-length-mismatch"
CODE_FRAME = "frame-mismatch"
CODE_TRACK = "track-mismatch"
CODE_MEDIA = "media-path-mismatch"
CODE_LINK = "link-mismatch"
CODE_MISSING = "missing-item"


class DeltaRow(StrictModel):
    item_id: str
    kind: TrackKind
    source_start_delta: int
    source_end_delta: int
    record_start_delta: int
    record_end_delta: int
    track_index_delta: int
    media_match: bool
    link_match: bool


class Mismatch(StrictModel):
    code: str
    detail: str


class CompareOutcome(StrictModel):
    passed: bool
    max_source_delta: int
    max_record_delta: int
    media_all_match: bool
    track_all_match: bool
    link_all_match: bool
    rows: tuple[DeltaRow, ...]
    mismatches: tuple[Mismatch, ...]


def _count_mismatch(mismatches: list[Mismatch], label: str, expected: int, actual: int) -> None:
    if expected != actual:
        mismatches.append(
            Mismatch(code=CODE_TRACK_COUNT, detail=f"{label} tracks {expected}->{actual}")
        )


def compare(expected: ExpectedBaseCut, snapshot: TimelineSnapshot) -> CompareOutcome:
    mismatches: list[Mismatch] = []
    _count_mismatch(mismatches, "video", expected.video_track_count, snapshot.video_track_count)
    _count_mismatch(mismatches, "audio", expected.audio_track_count, snapshot.audio_track_count)
    _count_mismatch(
        mismatches, "subtitle", expected.subtitle_track_count, snapshot.subtitle_track_count
    )
    if expected.record_frame_count != snapshot.record_frame_count:
        mismatches.append(
            Mismatch(
                code=CODE_RECORD_LENGTH,
                detail=(
                    f"record frames {expected.record_frame_count}->{snapshot.record_frame_count}"
                ),
            )
        )
    rows: list[DeltaRow] = []
    for kind in KINDS:
        rows.extend(_compare_kind(expected, snapshot, kind, mismatches))
    return CompareOutcome(
        passed=not mismatches,
        max_source_delta=max(
            (max(abs(row.source_start_delta), abs(row.source_end_delta)) for row in rows), default=0
        ),
        max_record_delta=max(
            (max(abs(row.record_start_delta), abs(row.record_end_delta)) for row in rows),
            default=0,
        ),
        media_all_match=bool(rows) and all(row.media_match for row in rows),
        track_all_match=bool(rows) and all(row.track_index_delta == 0 for row in rows),
        link_all_match=bool(rows) and all(row.link_match for row in rows),
        rows=tuple(rows),
        mismatches=tuple(mismatches),
    )


def _compare_kind(
    expected: ExpectedBaseCut, snapshot: TimelineSnapshot, kind: str, mismatches: list[Mismatch]
) -> list[DeltaRow]:
    expected_items = sorted(
        (item for item in expected.items if item.kind == kind), key=lambda item: item.record_start
    )
    built_items = [item for item in snapshot.items if item.kind == kind]
    if len(expected_items) != len(built_items):
        mismatches.append(
            Mismatch(
                code=CODE_ITEM_COUNT,
                detail=f"{kind} items {len(expected_items)}->{len(built_items)}",
            )
        )
    partners = {item.record_start: item for item in snapshot.items if item.kind != kind}
    rows: list[DeltaRow] = []
    for want in expected_items:
        built = next(
            (item for item in built_items if item.record_start == want.record_start), None
        )
        if built is None:
            mismatches.append(
                Mismatch(
                    code=CODE_MISSING,
                    detail=f"{kind} {want.item_id} at record {want.record_start} not built",
                )
            )
            continue
        _frame_checks(want, built, mismatches)
        partner = partners.get(want.record_start)
        link_ok = partner is not None and built.linked_ids == frozenset({partner.unique_id})
        if not link_ok:
            mismatches.append(
                Mismatch(
                    code=CODE_LINK,
                    detail=f"{kind} {want.item_id} links {sorted(built.linked_ids)}",
                )
            )
        media_ok = os.path.realpath(built.media_path) == want.media_path
        if not media_ok:
            mismatches.append(
                Mismatch(
                    code=CODE_MEDIA,
                    detail=f"{kind} {want.item_id} media {built.media_path} != {want.media_path}",
                )
            )
        if built.track_index != want.track_index:
            mismatches.append(
                Mismatch(
                    code=CODE_TRACK,
                    detail=f"{kind} {want.item_id} track {want.track_index}->{built.track_index}",
                )
            )
        rows.append(
            DeltaRow(
                item_id=want.item_id,
                kind=want.kind,
                source_start_delta=built.source_start - want.source_start,
                source_end_delta=built.source_end - want.source_end,
                record_start_delta=built.record_start - want.record_start,
                record_end_delta=built.record_end - want.record_end,
                track_index_delta=built.track_index - want.track_index,
                media_match=media_ok,
                link_match=link_ok,
            )
        )
    return rows


def _frame_checks(want: ExpectedItem, built: ReadItem, mismatches: list[Mismatch]) -> None:
    deltas = (
        ("source_start", built.source_start - want.source_start),
        ("source_end", built.source_end - want.source_end),
        ("record_start", built.record_start - want.record_start),
        ("record_end", built.record_end - want.record_end),
    )
    for field, delta in deltas:
        if delta != 0:
            mismatches.append(
                Mismatch(
                    code=CODE_FRAME,
                    detail=(
                        f"{want.kind} {want.item_id} {field} "
                        f"{getattr(want, field)}->{getattr(built, field)} (delta {delta})"
                    ),
                )
            )


def summary_line(outcome: CompareOutcome) -> str:
    return (
        f"base-cut: requested=built={str(outcome.passed).lower()} "
        f"items={len(outcome.rows)}/{len(outcome.rows)} "
        f"media-match={str(outcome.media_all_match).lower()} "
        f"track-match={str(outcome.track_all_match).lower()} "
        f"link-match={str(outcome.link_all_match).lower()} "
        f"source-delta={outcome.max_source_delta} "
        f"record-delta={outcome.max_record_delta}"
    )


def delta_lines(outcome: CompareOutcome) -> list[str]:
    return [
        (
            f"base-cut: item={row.item_id} kind={row.kind} "
            f"dsrc={row.source_start_delta}/{row.source_end_delta} "
            f"drec={row.record_start_delta}/{row.record_end_delta} "
            f"dtrack={row.track_index_delta} "
            f"media={'match' if row.media_match else 'MISMATCH'} "
            f"link={'match' if row.link_match else 'MISMATCH'}"
        )
        for row in outcome.rows
    ]


class BaseCutReport(StrictModel):
    schema_version: str
    requested: ExpectedBaseCut
    built: TimelineSnapshot
    outcome: CompareOutcome
