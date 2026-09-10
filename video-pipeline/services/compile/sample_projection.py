"""Pure sample-window projection over a compiled TimelineIr0C (wave 1).

The 30-second 試し動画 (sample) is a REBUILDABLE preview artifact: the
operator's adopted policy is committed as a normal validated plan/IR
first, and the sample is produced by projecting explicit Record-space
half-open windows of that UNCHANGED full IR — the full plan/IR hashes
are never touched (2026-09-10 v4 contract, 必須訂正1/2).

Coordinate contract: windows are Record-frame half-open intervals ON THE
FULL IR. Original coordinates never appear here; source spans shift by
the clip amounts only (no scaling). Video/audio tracks must re-lay
gapless over [0, total); the subtitle track allows gaps between cues,
forbids overlaps, and is OMITTED entirely when no cue intersects.

Identity contract (F5): windows are NORMALIZED to canonical sorted order
before anything derives from them, so a reorder-only difference yields
the same sample identity (no double charge) in both
``sample_identity_digest`` (sample_identity) and this projection; the
artifact id carries the FULL window digest (no truncation — different
windows always yield a different id) and the content hash binds the
full-IR hash to that same digest.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from itertools import pairwise

from pydantic_core import PydanticCustomError

from services.contracts.primitives import RationalFrameRate, SourceFrameSpan, SourceRef
from services.contracts.timeline_ir import (
    RecordFrameSpan,
    TimelineIr0C,
    TimelineItem0C,
    TimelineTrack0C,
)


def sample_total_frames(windows: Sequence[RecordFrameSpan]) -> int:
    """Deterministic total = sum of window lengths (half-open)."""
    return sum(w.end_frame - w.start_frame for w in windows)


def _merge_nearby_windows(
    windows: Sequence[RecordFrameSpan], gap_frames: float
) -> list[RecordFrameSpan]:
    """Coalesce windows that overlap or nearly touch (≤``gap_frames``
    apart, sorted first) into one contiguous span — the merged count is
    the real count shown to the operator."""
    ordered = sorted(windows, key=lambda w: (w.start_frame, w.end_frame))
    merged: list[RecordFrameSpan] = [ordered[0]]
    for window in ordered[1:]:
        if window.start_frame - merged[-1].end_frame <= gap_frames:
            merged[-1] = RecordFrameSpan(
                start_frame=merged[-1].start_frame,
                end_frame=max(merged[-1].end_frame, window.end_frame),
            )
        else:
            merged.append(window)
    return merged


def _cap_window_total(
    windows: Sequence[RecordFrameSpan], budget_frames: int, floor_frames: int
) -> list[RecordFrameSpan]:
    """Proportionally shrink ``windows`` (end-anchored) to
    ``budget_frames`` total, never below ``floor_frames`` per window;
    latest windows give way first when the floor saturates the budget."""
    total = sum(w.end_frame - w.start_frame for w in windows)
    if total <= budget_frames:
        return list(windows)
    capped: list[RecordFrameSpan] = []
    for window in windows:
        length = window.end_frame - window.start_frame
        take = min(length, max(length * budget_frames // total, floor_frames))
        capped.append(
            RecordFrameSpan(
                start_frame=window.end_frame - take,
                end_frame=window.end_frame,
            )
        )
    while sum(w.end_frame - w.start_frame for w in capped) > budget_frames:
        capped.pop()  # the real count shrinks
    return capped


def windows_around_anchors(
    ir: TimelineIr0C, anchors: Sequence[int], limit_seconds: float = 30.0
) -> tuple[RecordFrameSpan, ...]:
    """Widen record-frame ``anchors`` into contiguous ~8s spans (5-10s band).

    Shared widening base: position sampling picks head/middle/late anchors
    and GLM-observation candidates arrive as record-frame anchors — both
    widen here (merge ≤1s-apart spans, cap the total at ``limit_seconds``
    with a ≥1s floor, latest windows give way first)."""
    video = next((t for t in ir.tracks if t.track.kind == "video"), None)
    if video is None or not video.items:
        raise PydanticCustomError("sample-empty-track", "no video interval")
    if not anchors:
        raise PydanticCustomError("sample-windows-empty", "windows must not be empty")
    fps = float(ir.rate.num) / float(ir.rate.den)
    track_end = max(item.record_span.end_frame for item in video.items)
    span_frames = max(round(8.0 * fps), 1)
    half = span_frames // 2
    raw: list[RecordFrameSpan] = []
    for anchor in anchors:
        clamped = min(max(int(anchor), 0), track_end)
        start = max(clamped - half, 0)
        end = min(start + span_frames, track_end)
        if end >= track_end:  # keep the nominal length when an edge clamps
            start = max(end - span_frames, 0)
        raw.append(RecordFrameSpan(start_frame=start, end_frame=end))
    merged = _merge_nearby_windows(raw, gap_frames=fps)
    return tuple(
        _cap_window_total(
            merged,
            budget_frames=max(int(limit_seconds * fps), 0),
            floor_frames=max(round(1.0 * fps), 1),
        )
    )


def derive_sample_windows(
    ir: TimelineIr0C, limit_seconds: float = 30.0
) -> tuple[RecordFrameSpan, ...]:
    """Sample up to three POSITIONS (head/middle/late) of the committed
    IR's video track as CONTIGUOUS ~8s spans (5-10s band), not single
    short edit cuts. The honest unit is a 「か所」 (a place), never a
    「場面」 (a scene): position sampling only. Windows that overlap or
    nearly touch (≤1s apart) merge and the merged count is the real
    count; the total is kept within ``limit_seconds`` by proportional
    shrinking with a ≥1s floor (latest windows give way first).

    Legacy server-picking path: the consultation sample route now uses
    GLM-observation candidates widened through ``windows_around_anchors``
    instead of calling this; this stays for diagnostics and regressions.
    """
    video = next((t for t in ir.tracks if t.track.kind == "video"), None)
    if video is None or not video.items:
        raise PydanticCustomError("sample-empty-track", "no video interval")
    items = sorted(video.items, key=lambda i: i.record_span.start_frame)
    picks = [items[0], items[len(items) // 2], items[-1]]
    unique: list[TimelineItem0C] = []
    for pick in picks:
        if pick not in unique:
            unique.append(pick)
    return windows_around_anchors(
        ir,
        [pick.record_span.start_frame for pick in unique],
        limit_seconds,
    )


def sample_total_seconds(
    windows: Sequence[RecordFrameSpan], rate: RationalFrameRate
) -> float:
    """Total sample seconds under the IR's frame rate (budget gate input)."""
    return sample_total_frames(windows) * float(rate.den) / float(rate.num)


def _sorted_windows(windows: Sequence[RecordFrameSpan]) -> tuple[RecordFrameSpan, ...]:
    if not windows:
        raise PydanticCustomError("sample-windows-empty", "windows must not be empty")
    for window in windows:
        if window.end_frame <= window.start_frame:
            raise PydanticCustomError(
                "sample-window-empty-span",
                "sample windows must have positive length",
                {"start_frame": window.start_frame, "end_frame": window.end_frame},
            )
    ordered = tuple(sorted(windows, key=lambda w: (w.start_frame, w.end_frame)))
    for earlier, later in pairwise(ordered):
        if later.start_frame < earlier.end_frame:
            raise PydanticCustomError(
                "sample-window-overlap", "sample windows must not overlap"
            )
    return ordered


def window_digest(windows: Sequence[RecordFrameSpan]) -> str:
    """Full hex digest of the NORMALIZED window spec (order-independent)."""
    ordered = _sorted_windows(windows)
    spec = "|".join(f"{w.start_frame}:{w.end_frame}" for w in ordered)
    return hashlib.sha256(spec.encode()).hexdigest()


def _window_starts(ordered: Sequence[RecordFrameSpan]) -> tuple[int, ...]:
    """Cumulative record start of each window on the sample timeline."""
    starts: list[int] = []
    cursor = 0
    for window in ordered:
        starts.append(cursor)
        cursor += window.end_frame - window.start_frame
    return tuple(starts)


def _require_span_precondition(ir: TimelineIr0C) -> None:
    """Per-item input precondition BEFORE clipping (F5).

    Every full-IR item must already satisfy source-length ==
    record-length; a scaled/remapped item is a typed
    ``sample-item-span-mismatch`` failure, never silently re-timed.
    """
    for track in ir.tracks:
        for item in track.items:
            source_length = item.source.span.end_frame - item.source.span.start_frame
            record_length = (
                item.record_span.end_frame - item.record_span.start_frame
            )
            if source_length != record_length:
                raise PydanticCustomError(
                    "sample-item-span-mismatch",
                    "sample source span length must equal record span length "
                    "before clipping",
                    {"item_id": item.item_id},
                )


def project_sample_ir(ir: TimelineIr0C, windows: Sequence[RecordFrameSpan]) -> TimelineIr0C:
    """Project Record-space half-open ``windows`` of ``ir`` into a new IR.

    Deterministic and pure: the same ``(ir, normalized windows)`` pair
    always yields the byte-identical sample IR, and derived item ids are
    stable (``{item_id}.s{window_index}`` — one interval per item∩window,
    so no segment index is needed; the id charset stays within
    ``^[A-Za-z0-9][A-Za-z0-9._:-]*$``). Every failure is a typed
    ``PydanticCustomError`` raised BEFORE any rendering could start.
    """
    _require_span_precondition(ir)
    ordered = _sorted_windows(windows)
    starts = _window_starts(ordered)
    total = sample_total_frames(ordered)

    tracks: list[TimelineTrack0C] = []
    for track in ir.tracks:
        projected: list[TimelineItem0C] = []
        for item in track.items:
            for window_index, window in enumerate(ordered):
                r_start = max(item.record_span.start_frame, window.start_frame)
                r_end = min(item.record_span.end_frame, window.end_frame)
                if r_start >= r_end:
                    continue
                left_clip = r_start - item.record_span.start_frame
                right_clip = item.record_span.end_frame - r_end
                sample_start = starts[window_index] + (r_start - window.start_frame)
                projected.append(
                    TimelineItem0C(
                        item_id=f"{item.item_id}.s{window_index}",
                        kind=item.kind,
                        source=SourceRef(
                            source_id=item.source.source_id,
                            span=SourceFrameSpan(
                                start_frame=item.source.span.start_frame + left_clip,
                                end_frame=item.source.span.end_frame - right_clip,
                                rate=item.source.span.rate,
                            ),
                        ),
                        record_span=RecordFrameSpan(
                            start_frame=sample_start,
                            end_frame=sample_start + (r_end - r_start),
                        ),
                        av_link_id=item.av_link_id,
                        subtitle_text=item.subtitle_text,
                    )
                )
        if not projected:
            if track.track.kind == "subtitle":
                continue  # 訂正2: a zero-cue sample omits the subtitle track
            raise PydanticCustomError(
                "sample-empty-track", "track has no item in any window"
            )
        projected.sort(key=lambda i: (i.record_span.start_frame, i.record_span.end_frame))
        _validate_track(projected, track.track.kind, total)
        tracks.append(
            TimelineTrack0C(track=track.track, items=tuple(projected))
        )

    if not tracks:
        raise PydanticCustomError("sample-empty-track", "no track intersects any window")
    # NOTE (audit): the old sample-av-length-mismatch totals check is gone
    # because it is unreachable — _validate_track above already refuses any
    # video/audio track whose items do not re-lay gapless over exactly
    # [0, total), so every surviving A/V kind sums to total by construction.
    digest = window_digest(ordered)
    _require_av_pairing(tracks, total)
    return TimelineIr0C(
        artifact_id=f"{ir.artifact_id}-sample-{digest}",
        artifact_type="timeline_ir_0c",
        schema_version=ir.schema_version,
        # source-descriptive digest (phase0c precedent: hash of the
        # derivation input, not of this envelope): full-IR hash + the FULL
        # normalized window digest (the artifact_id suffix above carries
        # the same full digest, untruncated).
        content_hash=hashlib.sha256(
            f"{ir.content_hash}:{digest}".encode()
        ).hexdigest(),
        producer=ir.producer,
        inputs=ir.inputs,
        rate=ir.rate,
        tracks=tuple(tracks),
    )


def _validate_track(
    items: Sequence[TimelineItem0C], kind: str, total: int
) -> None:
    if kind == "subtitle":
        for cue, nxt in pairwise(items):
            if nxt.record_span.start_frame < cue.record_span.end_frame:
                raise PydanticCustomError(
                    "sample-subtitle-overlap", "subtitle cues overlap",
                )
        # NOTE (audit): the old sample-subtitle-out-of-range check is gone
        # because it is unreachable — every cue is built by clamping a
        # source cue to its window and re-laying at starts[window] +
        # offset, so cue end <= starts[i] + window length <= total.
        return
    cursor = 0
    for item in items:
        if item.record_span.start_frame != cursor:
            code = (
                "sample-track-gap"
                if item.record_span.start_frame > cursor
                else "sample-track-overlap"
            )
            raise PydanticCustomError(code, "items must re-lay gapless from zero")
        cursor = item.record_span.end_frame
    if cursor != total:
        raise PydanticCustomError(
            "sample-track-gap", "track coverage ends before the window total",
        )


def _require_av_pairing(tracks: Sequence[TimelineTrack0C], total: int) -> None:
    """AV pairing (P1-6): video and audio over the SAME record interval.

    The sample renderer contract needs EXACTLY ONE video track and
    EXACTLY ONE audio track: a missing kind, multiple tracks of a
    kind, or a pairing mismatch all stop typed BEFORE any render —
    never a silent mis-pair and never a first-track-only inspection.
    Every atomic record interval must then be covered by exactly one
    video span and one audio span carrying the SAME non-null
    av_link_id. The subtitle track never participates.
    """
    videos = [t for t in tracks if t.track.kind == "video"]
    audios = [t for t in tracks if t.track.kind == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        raise PydanticCustomError(
            "sample-av-track-count",
            "sample needs exactly one video track and one audio track",
            {
                "video_tracks": len(videos),
                "audio_tracks": len(audios),
            },
        )
    video, audio = videos[0], audios[0]
    cuts = {0, total}
    for track in (video, audio):
        for item in track.items:
            cuts.add(item.record_span.start_frame)
            cuts.add(item.record_span.end_frame)
    for start, end in pairwise(sorted(cuts)):
        v_link = next(
            i.av_link_id
            for i in video.items
            if i.record_span.start_frame <= start and end <= i.record_span.end_frame
        )
        a_link = next(
            i.av_link_id
            for i in audio.items
            if i.record_span.start_frame <= start and end <= i.record_span.end_frame
        )
        if v_link is None or a_link is None or v_link != a_link:
            raise PydanticCustomError(
                "sample-av-pairing-mismatch",
                "video and audio over the same record interval must share one av_link_id",
                {"start_frame": start, "end_frame": end},
            )
