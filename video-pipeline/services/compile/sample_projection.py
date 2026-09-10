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


def derive_sample_windows(
    ir: TimelineIr0C, limit_seconds: float = 30.0
) -> tuple[RecordFrameSpan, ...]:
    """Pick up to three representative edit intervals (head/middle/late)
    from the committed IR's video track, capped to ``limit_seconds`` in
    total — the operator sees the adopted policy across DIFFERENT
    scenes, not just the opening (2026-09-10 main-path review)."""
    video = next((t for t in ir.tracks if t.track.kind == "video"), None)
    if video is None or not video.items:
        raise PydanticCustomError("sample-empty-track", "no video interval")
    items = sorted(video.items, key=lambda i: i.record_span.start_frame)
    picks = [items[0], items[len(items) // 2], items[-1]]
    unique: list[TimelineItem0C] = []
    for pick in picks:
        if pick not in unique:
            unique.append(pick)
    per_cap = max(
        int(limit_seconds * ir.rate.den / ir.rate.num / len(unique)), 1
    )
    windows: list[RecordFrameSpan] = []
    for item in unique:
        start = item.record_span.start_frame
        end = min(item.record_span.end_frame, start + per_cap)
        windows.append(RecordFrameSpan(start_frame=start, end_frame=end))
    return tuple(windows)


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
