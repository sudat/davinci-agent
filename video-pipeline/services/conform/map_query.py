"""Typed Original <-> Edit conversion API over a committed ConformMap.

Point queries use the frozen assignment rules from ``services.conform.convert``
(never ad hoc seconds math); span queries preserve half-open ``[start, end)``
semantics and are drop-aware: an original span that crosses frames the
normalization dropped is refused with ``CoordinateRangeError`` so callers
subdivide per the accounting instead of receiving a silently-shortened span.
Out-of-range positions are explicit errors, never clamps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.conform.convert import video_frame_for_pts
from services.conform.errors import CoordinateRangeError
from services.conform.map_models import AudioAffineMap, ConformMap, EditFrameSpan

if TYPE_CHECKING:
    from services.conform.coordinates import OriginalTimestamp, PtsSpan


def _first_row_at_or_after(conform_map: ConformMap, source_frame: int) -> int:
    for row in conform_map.video_table.rows:
        if row.source_frame >= source_frame:
            return row.edit_frame
    return conform_map.video_table.output_frames


def original_pts_to_edit_frame(
    conform_map: ConformMap, timestamp: OriginalTimestamp
) -> int:
    table = conform_map.video_table
    source_frame = video_frame_for_pts(timestamp, table.source_rate)
    if source_frame >= table.source_frame_count:
        raise CoordinateRangeError(
            f"pts {timestamp.pts} maps beyond the last source frame "
            f"({table.source_frame_count})"
        )
    if source_frame in set(conform_map.normalization.dropped_source_frames):
        raise CoordinateRangeError(
            f"source frame {source_frame} is dropped at the target rate; "
            "the drop/dup accounting must drive any placement decision"
        )
    return _first_row_at_or_after(conform_map, source_frame)


def edit_frame_to_original_pts(
    conform_map: ConformMap, edit_frame: int
) -> OriginalTimestamp:
    rows = conform_map.video_table.rows
    if not 0 <= edit_frame < len(rows):
        raise CoordinateRangeError(
            f"edit frame {edit_frame} outside half-open table "
            f"[0, {conform_map.video_table.output_frames})"
        )
    return rows[edit_frame].original_pts


def original_span_to_edit_span(conform_map: ConformMap, span: PtsSpan) -> EditFrameSpan:
    table = conform_map.video_table
    count = table.source_frame_count
    start_frame = video_frame_for_pts(span.start, table.source_rate)
    end_frame = min(video_frame_for_pts(span.end, table.source_rate), count)
    if start_frame > end_frame:
        raise CoordinateRangeError(
            f"span [{span.start_pts}, {span.end_pts}) inverts to source frames "
            f"[{start_frame}, {end_frame})"
        )
    dropped = sorted(
        frame
        for frame in conform_map.normalization.dropped_source_frames
        if start_frame <= frame < end_frame
    )
    if dropped:
        raise CoordinateRangeError(
            f"span [{span.start_pts}, {span.end_pts}) crosses dropped source "
            f"frames {dropped}; subdivide per the drop/dup accounting"
        )
    start_edit = _first_row_at_or_after(conform_map, start_frame)
    if start_frame == end_frame:
        return EditFrameSpan(start_frame=start_edit, end_frame=start_edit)
    end_edit = _first_row_at_or_after(conform_map, end_frame)
    return EditFrameSpan(start_frame=start_edit, end_frame=end_edit)


def original_sample_to_edit_sample(conform_map: ConformMap, sample: int) -> int:
    audio = conform_map.audio_map
    if isinstance(audio, AudioAffineMap):
        if not 0 <= sample < audio.original_sample_count:
            raise CoordinateRangeError(
                f"sample {sample} outside original audio "
                f"[0, {audio.original_sample_count})"
            )
        edit = sample - audio.origin_original_sample + audio.origin_edit_sample
        if not 0 <= edit < audio.edit_sample_count:
            raise CoordinateRangeError(
                f"sample {sample} maps outside edit audio [0, {audio.edit_sample_count})"
            )
        return edit
    for pair in audio.pairs:
        if pair.original_sample == sample:
            return pair.edit_sample
    raise CoordinateRangeError(
        f"sample {sample} is not an explicit audio table anchor; "
        "segment interpolation is not frozen in map v1"
    )


def edit_sample_to_original_sample(conform_map: ConformMap, sample: int) -> int:
    audio = conform_map.audio_map
    if isinstance(audio, AudioAffineMap):
        if not 0 <= sample < audio.edit_sample_count:
            raise CoordinateRangeError(
                f"sample {sample} outside edit audio [0, {audio.edit_sample_count})"
            )
        original = sample - audio.origin_edit_sample + audio.origin_original_sample
        if not 0 <= original < audio.original_sample_count:
            raise CoordinateRangeError(
                f"sample {sample} maps outside original audio "
                f"[0, {audio.original_sample_count})"
            )
        return original
    for pair in audio.pairs:
        if pair.edit_sample == sample:
            return pair.original_sample
    raise CoordinateRangeError(
        f"sample {sample} is not an explicit audio table anchor; "
        "segment interpolation is not frozen in map v1"
    )
