"""Strict verdict and table models for build conformance.

The conformance table is item-structured BY CONSTRUCTION: the per-item
verdict list is a required min-length-one field, aggregate counts/totals
ride ALONGSIDE the item table (never instead of it), and every verdict's
``faults``/``passed``/delta fields plus the table's ``all_passed`` and
``missing_item_ids`` are RECOMPUTED by model validators from the raw match
fields — a caller cannot declare success the underlying fields do not
support, and a totals-only table cannot exist.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from services.contracts.primitives import Sha256, StrictModel, TrackKind

ItemFaultKind = Literal[
    "item_media_mismatch", "off_by_one", "wrong_track", "wrong_link", "missing_item"
]
TableFaultKind = Literal[
    "item_media_mismatch",
    "off_by_one",
    "wrong_track",
    "wrong_link",
    "missing_item",
    "extra_item",
]


def shape_faults(
    *, observed: bool, media_match: bool, span_match: bool, track_match: bool, link_match: bool
) -> tuple[ItemFaultKind, ...]:
    """The fault shape implied by the raw match flags, in canonical order."""

    if not observed:
        return ("missing_item",)
    faults: list[ItemFaultKind] = []
    if not media_match:
        faults.append("item_media_mismatch")
    if not span_match:
        faults.append("off_by_one")
    if not track_match:
        faults.append("wrong_track")
    if not link_match:
        faults.append("wrong_link")
    return tuple(faults)


class ItemVerdict(StrictModel):
    """One package placement vs its observed readback row, field by field."""

    item_id: str = Field(min_length=1)
    unique_id: str | None = None
    observed: bool
    media_match: bool
    media_path_expected: str
    media_path_observed: str
    media_sha256_expected: Sha256
    media_sha256_observed: Sha256
    span_match: bool
    source_start_expected: int = Field(ge=0, strict=True)
    source_start_observed: int = Field(ge=0, strict=True)
    source_end_expected: int = Field(gt=0, strict=True)
    source_end_observed: int = Field(gt=0, strict=True)
    record_start_expected: int = Field(ge=0, strict=True)
    record_start_observed: int = Field(ge=0, strict=True)
    record_end_expected: int = Field(gt=0, strict=True)
    record_end_observed: int = Field(gt=0, strict=True)
    source_start_delta_frames: int = 0
    source_end_delta_frames: int = 0
    record_start_delta_frames: int = 0
    record_end_delta_frames: int = 0
    track_match: bool
    track_kind_expected: TrackKind
    track_kind_observed: TrackKind
    track_index_expected: int = Field(gt=0, strict=True)
    track_index_observed: int = Field(gt=0, strict=True)
    link_match: bool
    linked_expected: tuple[str, ...] = Field(default=())
    linked_observed: tuple[str, ...] = Field(default=())
    faults: tuple[ItemFaultKind, ...] = Field(default=())
    passed: bool
    detail: str = ""

    @model_validator(mode="after")
    def enforce_derived_consistency(self) -> ItemVerdict:
        """Deltas/faults/passed must equal what the raw fields imply — lies are rejected."""

        derived_faults = shape_faults(
            observed=self.observed,
            media_match=self.media_match,
            span_match=self.span_match,
            track_match=self.track_match,
            link_match=self.link_match,
        )
        inconsistent = [
            f"{suffix}_delta_frames"
            for suffix in ("source_start", "source_end", "record_start", "record_end")
            if getattr(self, f"{suffix}_delta_frames")
            != getattr(self, f"{suffix}_observed") - getattr(self, f"{suffix}_expected")
        ]
        if self.faults != derived_faults or self.passed != (not derived_faults) or inconsistent:
            raise PydanticCustomError(
                "derived_fields_inconsistent",
                "{item_id}: derived fields disagree with the raw match fields ({fields})",
                {"item_id": self.item_id, "fields": ", ".join(inconsistent) or "faults/passed"},
            )
        return self


class ExtraRow(StrictModel):
    """An observed readback row no package placement accounts for."""

    observed_id: str = Field(min_length=1)
    unique_id: str = Field(min_length=1)
    kind: TrackKind
    record_start: int = Field(ge=0, strict=True)
    record_end: int = Field(gt=0, strict=True)
    media_path: str = Field(min_length=1)
    faults: tuple[Literal["extra_item"], ...] = Field(default=("extra_item",))


class ConformanceTable(StrictModel):
    """Per-item verdicts plus aggregates; the item table is mandatory."""

    timeline_name: str = Field(min_length=1)
    package_artifact_id: str = Field(min_length=1)
    items: tuple[ItemVerdict, ...] = Field(min_length=1)
    extra_rows: tuple[ExtraRow, ...] = Field(default=())
    expected_items: int = Field(gt=0, strict=True)
    observed_items: int = Field(ge=0, strict=True)
    missing_item_ids: tuple[str, ...] = Field(default=())
    expected_total_record_frames: int = Field(ge=0, strict=True)
    observed_total_record_frames: int = Field(ge=0, strict=True)
    total_record_delta_frames: int = 0
    expected_fingerprint: Sha256
    observed_fingerprint: Sha256
    all_passed: bool

    @model_validator(mode="after")
    def enforce_derived_consistency(self) -> ConformanceTable:
        """Missing ids, totals delta, and all_passed must match the items — lies are rejected."""

        derived_missing = tuple(
            verdict.item_id for verdict in self.items if not verdict.observed
        )
        derived_delta = self.observed_total_record_frames - self.expected_total_record_frames
        derived_all = (
            bool(self.items)
            and not self.extra_rows
            and all(verdict.passed for verdict in self.items)
        )
        if (
            self.missing_item_ids != derived_missing
            or self.total_record_delta_frames != derived_delta
            or self.all_passed != derived_all
        ):
            raise PydanticCustomError(
                "derived_fields_inconsistent",
                "aggregate fields disagree with the per-item verdicts",
            )
        return self


__all__ = [
    "ConformanceTable",
    "ExtraRow",
    "ItemFaultKind",
    "ItemVerdict",
    "TableFaultKind",
    "shape_faults",
]
