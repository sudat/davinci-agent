"""Association of observed readback rows with package placements.

Pairing rule: exact ``(kind, record_start)`` matches bind first; when exactly
one same-kind placement remains unbound, it pairs with the nearest leftover
row of that kind (record-start distance). Rows no placement claims become
the extra-rows side of the conformance table; the association also projects
the observed table onto the canonical fingerprint placement shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.build.conformance_capture import READ_KINDS
from services.build.conformance_fingerprint import FingerprintPlacement

if TYPE_CHECKING:
    from services.build.conformance_capture import ReadbackRow
    from services.resolve_adapter.models import ResolvePackage


class Association:
    """Row↔placement pairing plus the leftover (extra) rows."""

    def __init__(
        self,
        row_of: dict[str, ReadbackRow],
        item_id_of: dict[str, str],
        extra_rows: tuple[ReadbackRow, ...],
    ) -> None:
        self.row_of = row_of
        self.item_id_of = item_id_of
        self.extra_rows = extra_rows

    def item_id_of_row(self, unique_id: str) -> str:
        return self.item_id_of.get(unique_id, f"observed:{unique_id}")


def associate(package: ResolvePackage, rows: tuple[ReadbackRow, ...]) -> Association:
    row_of: dict[str, ReadbackRow] = {}
    item_id_of: dict[str, str] = {}
    by_key: dict[tuple[str, int], list[ReadbackRow]] = {}
    for row in rows:
        by_key.setdefault((row.kind, row.record_start), []).append(row)
    pending = list(package.placements)
    for placement in package.placements:
        info = placement.clip_info
        candidates = [
            row
            for row in by_key.get((info.track_type, info.record_frame), ())
            if row.unique_id not in item_id_of
        ]
        if candidates:
            row_of[placement.item_id] = candidates[0]
            item_id_of[candidates[0].unique_id] = placement.item_id
            pending.remove(placement)
    for kind in READ_KINDS:
        wanted = [p for p in pending if p.clip_info.track_type == kind]
        if len(wanted) != 1:
            continue
        free = [row for row in rows if row.kind == kind and row.unique_id not in item_id_of]
        if not free:
            continue
        target = wanted[0].clip_info.record_frame
        nearest = min(free, key=lambda row: abs(row.record_start - target))
        row_of[wanted[0].item_id] = nearest
        item_id_of[nearest.unique_id] = wanted[0].item_id
    extras = tuple(row for row in rows if row.unique_id not in item_id_of)
    return Association(row_of, item_id_of, extras)


def observed_projection(
    rows: tuple[ReadbackRow, ...], association: Association
) -> tuple[FingerprintPlacement, ...]:
    return tuple(
        FingerprintPlacement(
            item_id=association.item_id_of_row(row.unique_id),
            kind=row.kind,
            track_index=row.track_index,
            record_start=row.record_start,
            record_end=row.record_end,
            source_start=row.source_start,
            source_end=row.source_start + row.source_duration,
            media_sha256=row.media_sha256,
            media_path=row.media_path,
            linked=tuple(
                sorted(
                    association.item_id_of.get(uid, uid)
                    for uid in row.linked_ids
                    if uid != row.unique_id
                )
            ),
        )
        for row in rows
    )


__all__ = ["Association", "associate", "observed_projection"]
