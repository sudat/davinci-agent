"""The canonical timeline fingerprint projection for build conformance.

The serialization adapts the Todo-18 canonical fingerprint to the conformance
table: placements ordered by ``(record_start, kind rank [video=0, audio=1],
item_id)``, one ``|``-separated line per placement

``item_id|kind|track_index|rec_start|rec_end|src_start|src_end|media_sha256|
media_path|links``

joined with ``\\n`` plus a trailing ``\\n``; the fingerprint is sha256 over
those UTF-8 bytes. Expected (package) and observed (readback) tables
serialize through the same projection — observed rows carry their associated
package item ids — so a conforming build yields byte-identical tables and
identical fingerprints.
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING, Final

from pydantic import Field

from services.contracts.primitives import Sha256, StrictModel, TrackKind

if TYPE_CHECKING:
    from services.resolve_adapter.models import ResolvePackage

KIND_RANK: Final[dict[str, int]] = {"video": 0, "audio": 1}
FIELD_SEPARATOR: Final = "|"
NO_LINK: Final = "-"


class FingerprintPlacement(StrictModel):
    """The common expected/observed projection the fingerprint serializes."""

    item_id: str = Field(min_length=1)
    kind: TrackKind
    track_index: int = Field(gt=0, strict=True)
    record_start: int = Field(ge=0, strict=True)
    record_end: int = Field(gt=0, strict=True)
    source_start: int = Field(ge=0, strict=True)
    source_end: int = Field(gt=0, strict=True)
    media_sha256: Sha256
    media_path: str = Field(min_length=1)
    linked: tuple[str, ...] = Field(default=())

    def line(self) -> str:
        return FIELD_SEPARATOR.join(
            (
                self.item_id,
                self.kind,
                str(self.track_index),
                str(self.record_start),
                str(self.record_end),
                str(self.source_start),
                str(self.source_end),
                self.media_sha256,
                self.media_path,
                ",".join(self.linked) if self.linked else NO_LINK,
            )
        )


def conformance_fingerprint(placements: tuple[FingerprintPlacement, ...]) -> Sha256:
    ordered = sorted(
        placements,
        key=lambda item: (item.record_start, KIND_RANK[item.kind], item.item_id),
    )
    return hashlib.sha256(("\n".join(item.line() for item in ordered) + "\n").encode()).hexdigest()


def link_partners(package: ResolvePackage) -> dict[str, frozenset[str]]:
    """item_id -> the other members of its package link group."""

    partners: dict[str, set[str]] = {}
    for group in package.link_groups:
        members = set(group.item_ids)
        for item_id in group.item_ids:
            partners.setdefault(item_id, set()).update(members - {item_id})
    return {item_id: frozenset(ids) for item_id, ids in partners.items()}


def expected_placements(package: ResolvePackage) -> tuple[FingerprintPlacement, ...]:
    """The package's canonical placement table (the expected projection)."""

    declared = {binding.source_id: binding for binding in package.inputs_view.declared_media}
    partners = link_partners(package)
    table: list[FingerprintPlacement] = []
    for placement in package.placements:
        info = placement.clip_info
        binding = declared.get(info.media_source_id)
        if binding is None:
            raise ValueError(f"{placement.item_id}: unbound media source {info.media_source_id}")
        table.append(
            FingerprintPlacement(
                item_id=placement.item_id,
                kind=info.track_type,
                track_index=info.track_index,
                record_start=info.record_frame,
                record_end=info.record_frame + (info.end_frame - info.start_frame),
                source_start=info.start_frame,
                source_end=info.end_frame,
                media_sha256=binding.sha256,
                media_path=os.path.realpath(binding.path),
                linked=tuple(sorted(partners.get(placement.item_id, frozenset()))),
            )
        )
    return tuple(table)


def expected_fingerprint(package: ResolvePackage) -> Sha256:
    return conformance_fingerprint(expected_placements(package))


__all__ = [
    "FingerprintPlacement",
    "conformance_fingerprint",
    "expected_fingerprint",
    "expected_placements",
    "link_partners",
]
