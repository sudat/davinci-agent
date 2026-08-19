"""Per-item verdict construction: one placement vs its observed row.

Every field pair is compared directly — media identity (resolved path +
file sha256), source span (StartFrame + Duration), record span, track
kind/index, and link-group membership — and the derived fault shape,
pass flag, and frame deltas are computed from those raw comparisons.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from services.build.conformance_models import ItemVerdict, shape_faults

if TYPE_CHECKING:
    from services.build.conformance_associate import Association
    from services.build.conformance_capture import ReadbackRow
    from services.resolve_adapter.models import AppendPlacement, ClipInfo, MediaBinding


def build_verdict(
    placement: AppendPlacement,
    binding: MediaBinding,
    partners: frozenset[str],
    association: Association,
) -> ItemVerdict:
    info = placement.clip_info
    expected_path = os.path.realpath(binding.path)
    expected_record_end = info.record_frame + (info.end_frame - info.start_frame)
    expected_links = tuple(sorted(partners))
    row = association.row_of.get(placement.item_id)
    if row is None:
        return _missing_verdict(placement, binding, expected_links, expected_record_end)
    observed_links = tuple(
        sorted(association.item_id_of.get(uid, uid) for uid in row.linked_ids)
    )
    source_end = row.source_start + row.source_duration
    media_match = row.media_path == expected_path and row.media_sha256 == binding.sha256
    span_match = (
        row.source_start == info.start_frame
        and source_end == info.end_frame
        and row.record_start == info.record_frame
        and row.record_end == expected_record_end
    )
    track_match = row.kind == info.track_type and row.track_index == info.track_index
    link_match = observed_links == expected_links
    faults = shape_faults(
        observed=True,
        media_match=media_match,
        span_match=span_match,
        track_match=track_match,
        link_match=link_match,
    )
    return ItemVerdict(
        item_id=placement.item_id,
        unique_id=row.unique_id,
        observed=True,
        media_match=media_match,
        media_path_expected=expected_path,
        media_path_observed=row.media_path,
        media_sha256_expected=binding.sha256,
        media_sha256_observed=row.media_sha256,
        span_match=span_match,
        source_start_expected=info.start_frame,
        source_start_observed=row.source_start,
        source_end_expected=info.end_frame,
        source_end_observed=source_end,
        record_start_expected=info.record_frame,
        record_start_observed=row.record_start,
        record_end_expected=expected_record_end,
        record_end_observed=row.record_end,
        source_start_delta_frames=row.source_start - info.start_frame,
        source_end_delta_frames=source_end - info.end_frame,
        record_start_delta_frames=row.record_start - info.record_frame,
        record_end_delta_frames=row.record_end - expected_record_end,
        track_match=track_match,
        track_kind_expected=info.track_type,
        track_kind_observed=row.kind,
        track_index_expected=info.track_index,
        track_index_observed=row.track_index,
        link_match=link_match,
        linked_expected=expected_links,
        linked_observed=row.linked_ids,
        faults=faults,
        passed=not faults,
        detail=_problems(row, binding, info, expected_path, expected_record_end,
                         expected_links, observed_links, source_end),
    )


def _missing_verdict(
    placement: AppendPlacement,
    binding: MediaBinding,
    expected_links: tuple[str, ...],
    expected_record_end: int,
) -> ItemVerdict:
    info = placement.clip_info
    expected_path = os.path.realpath(binding.path)
    return ItemVerdict(
        item_id=placement.item_id,
        observed=False,
        media_match=False,
        media_path_expected=expected_path,
        media_path_observed=expected_path,
        media_sha256_expected=binding.sha256,
        media_sha256_observed=binding.sha256,
        span_match=False,
        source_start_expected=info.start_frame,
        source_start_observed=info.start_frame,
        source_end_expected=info.end_frame,
        source_end_observed=info.end_frame,
        record_start_expected=info.record_frame,
        record_start_observed=info.record_frame,
        record_end_expected=expected_record_end,
        record_end_observed=expected_record_end,
        track_match=False,
        track_kind_expected=info.track_type,
        track_kind_observed=info.track_type,
        track_index_expected=info.track_index,
        track_index_observed=info.track_index,
        link_match=False,
        linked_expected=expected_links,
        linked_observed=(),
        faults=("missing_item",),
        passed=False,
        detail="no readback row matched this placement",
    )


def _problems(  # noqa: PLR0913, PLR0917 (one literal line per compared field)
    row: ReadbackRow,
    binding: MediaBinding,
    info: ClipInfo,
    expected_path: str,
    expected_record_end: int,
    expected_links: tuple[str, ...],
    observed_links: tuple[str, ...],
    source_end: int,
) -> str:
    problems: list[str] = []
    if row.media_path != expected_path:
        problems.append(f"media path {row.media_path} != {expected_path}")
    if row.media_sha256 != binding.sha256:
        problems.append(f"media sha256 {row.media_sha256} != declared {binding.sha256}")
    if row.source_start != info.start_frame or source_end != info.end_frame:
        problems.append(
            f"source span [{row.source_start},{source_end}) "
            f"!= [{info.start_frame},{info.end_frame})"
        )
    if row.record_start != info.record_frame or row.record_end != expected_record_end:
        problems.append(
            f"record span [{row.record_start},{row.record_end}) "
            f"!= [{info.record_frame},{expected_record_end})"
        )
    if row.kind != info.track_type:
        problems.append(f"track kind {row.kind} != {info.track_type}")
    if row.track_index != info.track_index:
        problems.append(f"track index {row.track_index} != {info.track_index}")
    if observed_links != expected_links:
        problems.append(f"links {list(observed_links)} != {list(expected_links)}")
    return "; ".join(problems)
